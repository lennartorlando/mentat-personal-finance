import csv
import importlib
import io
import json
import multiprocessing
import os
import queue
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from personal_finance_agent.aqbanking import parse_balance_output
from personal_finance_agent.cli import build_parser, cmd_aq_balances, cmd_ledger_export, cmd_ledger_inspect
from personal_finance_agent.ledger import (
    HoldingRecord,
    JsonlLedger,
    LedgerValidationError,
    Provenance,
    account_record,
    balance_record_from_row,
    export_balances_csv,
    holding_record,
    transaction_records,
)
from personal_finance_agent.ledger.models import stable_id
from personal_finance_agent.private_files import fsync_parent_directory

if os.name == "posix":
    import fcntl


IMPORTED_AT = datetime(2026, 9, 2, 8, 30, tzinfo=timezone.utc)


def _add_record_after_signal(
    ledger_path: str, record, ready, start, results
) -> None:
    ready.put(True)
    start.wait()
    result = JsonlLedger(Path(ledger_path)).add([record])
    results.put((result.added, result.duplicates))


def _try_nonblocking_ledger_lock(ledger_path: str, results) -> None:
    lock_path = Path(ledger_path).with_name(f".{Path(ledger_path).name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            results.put(False)
        else:
            results.put(True)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _export_after_validation_signal(
    csv_path: str,
    ledger_path: str,
    record,
    validated,
    release,
    results,
) -> None:
    export_module = importlib.import_module("personal_finance_agent.ledger.export")
    actual_reject = export_module.reject_unsafe_csv_overwrite

    def signal_after_validation(path, source_ledger_path):
        actual_reject(path, source_ledger_path)
        validated.put(True)
        if release is not None:
            release.wait()

    export_module.reject_unsafe_csv_overwrite = signal_after_validation
    try:
        export_module.export_balances_csv(
            Path(csv_path), [record], ledger_path=Path(ledger_path)
        )
    except ValueError:
        results.put("rejected")
    else:
        results.put("exported")


def balance_row() -> dict[str, str]:
    return {
        "date": "2026-09-02",
        "source": "AqBanking",
        "balance_date": "02.09.2026",
        "balance": "123.45 EUR",
        "iban": "",
        "bank_code": "synthetic-bank",
        "account_number": "synthetic-account",
        "exported_at": IMPORTED_AT.isoformat(),
    }


def formula_prefix_balance_records():
    records = []
    for day, prefix in enumerate(("=", "+", "-", "@", "\t", "\r"), start=1):
        row = balance_row()
        row["balance_date"] = f"2026-09-{day:02d}"
        row["balance"] = f"{prefix}danger"
        records.append(balance_record_from_row(row))
    return records


class LedgerRecordTests(unittest.TestCase):
    def test_aqbanking_balance_becomes_record_with_source_provenance(self):
        record = balance_record_from_row(balance_row())

        self.assertEqual(record.record_type, "balance")
        self.assertEqual(record.amount, "123.45 EUR")
        self.assertEqual(record.provenance.source, "AqBanking")
        self.assertEqual(record.provenance.source_ref, "aqbanking:listbal")
        self.assertTrue(record.id.startswith("bal_"))

    def test_transaction_identifier_is_stable_and_keeps_identical_payments_distinct(self):
        provenance = Provenance("synthetic-csv", IMPORTED_AT, "fixture.csv")
        rows = [
            {
                "account_id": "acct_synthetic",
                "booking_date": "2026-09-01",
                "amount": "-4.50 EUR",
                "purpose": "Morning   Coffee",
            },
            {
                "account_id": "acct_synthetic",
                "booking_date": "2026-09-01",
                "amount": "-4.50 EUR",
                "purpose": "Morning   Coffee",
            },
        ]

        first = transaction_records(rows, provenance, complete_source_slice=True)
        repeated = transaction_records(rows, provenance, complete_source_slice=True)

        self.assertEqual([record.id for record in first], [record.id for record in repeated])
        self.assertNotEqual(first[0].id, first[1].id)
        self.assertEqual(first[0].normalized_purpose, "morning coffee")
        self.assertEqual(first[0].purpose, "Morning   Coffee")
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            first_result = ledger.add(first)
            replay_result = ledger.add(repeated)

            self.assertEqual((first_result.added, replay_result.duplicates), (2, 2))
            self.assertEqual(
                [record.id for record in ledger.records("transaction")],
                [record.id for record in first],
            )

    def test_transaction_identifiers_do_not_depend_on_distinct_row_order(self):
        provenance = Provenance("synthetic-csv", IMPORTED_AT, "fixture.csv")
        coffee = {
            "account_id": "acct_synthetic",
            "booking_date": "2026-09-01",
            "amount": "-4.50 EUR",
            "purpose": "Morning   Coffee",
        }
        groceries = {
            "account_id": "acct_synthetic",
            "booking_date": "2026-09-01",
            "amount": "-28.00 EUR",
            "purpose": "Groceries",
        }

        first = transaction_records(
            [coffee, groceries, coffee], provenance, complete_source_slice=True
        )
        reordered = transaction_records(
            [groceries, coffee, coffee], provenance, complete_source_slice=True
        )

        self.assertEqual({record.id for record in first}, {record.id for record in reordered})
        self.assertEqual(sorted(record.ordinal for record in first), [0, 0, 1])
        self.assertEqual(first[0].purpose, "Morning   Coffee")

    def test_transaction_records_reject_incremental_source_batches(self):
        provenance = Provenance("synthetic-csv", IMPORTED_AT, "fixture.csv")

        with self.assertRaisesRegex(ValueError, "requires a complete source slice"):
            transaction_records([], provenance, complete_source_slice=False)

    def test_balance_requires_complete_fallback_account_identifier(self):
        for present, missing in (
            ("bank_code", "account_number"),
            ("account_number", "bank_code"),
        ):
            with self.subTest(present=present):
                row = balance_row()
                row["bank_code"] = ""
                row["account_number"] = ""
                row[present] = "synthetic-identifier"

                with self.assertRaises(LedgerValidationError) as raised:
                    balance_record_from_row(row)

                self.assertEqual([item.field for item in raised.exception.diagnostics], [missing])

    def test_duplicate_import_leaves_jsonl_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            record = balance_record_from_row(balance_row())

            first = ledger.add([record])
            original_bytes = ledger.path.read_bytes()
            second = ledger.add([record])

            self.assertEqual((first.added, first.duplicates), (1, 0))
            self.assertEqual((second.added, second.duplicates), (0, 1))
            self.assertEqual(ledger.path.read_bytes(), original_bytes)
            self.assertEqual([stored.id for stored in ledger.records()], [record.id])

    def test_changed_same_day_balance_replaces_stale_record_under_stable_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            original = balance_record_from_row(balance_row())
            corrected_row = balance_row()
            corrected_row["balance"] = "145.67 EUR"
            corrected_row["exported_at"] = "2026-09-02T09:30:00+00:00"
            corrected = balance_record_from_row(corrected_row)
            self.assertEqual(corrected.id, original.id)

            ledger.add([original])
            result = ledger.add([corrected])

            stored = ledger.records("balance")
            self.assertEqual((result.added, result.updated, result.duplicates), (0, 1, 0))
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0].id, original.id)
            self.assertEqual(stored[0].amount, "145.67 EUR")

    def test_changed_balance_amount_returns_structured_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            original = balance_record_from_row(balance_row())
            corrected_row = balance_row()
            corrected_row["balance"] = "145.67 EUR"
            corrected = balance_record_from_row(corrected_row)
            ledger.add([original])

            result = ledger.add([corrected])

            self.assertEqual(len(result.diagnostics), 1)
            self.assertEqual(
                result.diagnostics[0].to_dict(),
                {
                    "input": "aqbanking:listbal",
                    "field": "amount",
                    "message": "balance amount changed",
                    "record_id": original.id,
                    "previous_value": "123.45 EUR",
                    "new_value": "145.67 EUR",
                },
            )

    def test_changed_transaction_provenance_returns_structured_diagnostic(self):
        first_provenance = Provenance("synthetic-csv", IMPORTED_AT, "first.csv")
        second_provenance = Provenance("synthetic-csv", IMPORTED_AT, "second.csv")
        rows = [{
            "account_id": "acct_synthetic",
            "booking_date": "2026-09-01",
            "amount": "-10.00 EUR",
            "purpose": "Auditable transaction",
        }]
        original = transaction_records(
            rows, first_provenance, complete_source_slice=True
        )[0]
        reassigned = transaction_records(
            rows, second_provenance, complete_source_slice=True
        )[0]
        self.assertEqual(reassigned.id, original.id)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            ledger.add([original])

            result = ledger.add([reassigned])

            self.assertEqual((result.added, result.updated, result.duplicates), (0, 1, 0))
            self.assertEqual(ledger.records("transaction"), [reassigned])
            self.assertEqual(
                [diagnostic.to_dict() for diagnostic in result.diagnostics],
                [{
                    "input": "second.csv",
                    "field": "provenance.source_ref",
                    "message": "record provenance changed",
                    "record_id": original.id,
                    "previous_value": "first.csv",
                    "new_value": "second.csv",
                }],
            )

    def test_imported_at_only_change_is_duplicate_and_leaves_jsonl_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            original = balance_record_from_row(balance_row())
            replay_row = balance_row()
            replay_row["exported_at"] = "2026-09-02T09:30:00+00:00"
            replay = balance_record_from_row(replay_row)
            ledger.add([original])
            original_bytes = ledger.path.read_bytes()

            result = ledger.add([replay])

            self.assertEqual((result.added, result.duplicates), (0, 1))
            self.assertEqual(ledger.path.read_bytes(), original_bytes)

    def test_balance_account_identifiers_are_immutable_and_still_exportable(self):
        record = balance_record_from_row(balance_row())

        with self.assertRaises(TypeError):
            record.account_identifiers["account_number"] = "changed"  # type: ignore[index]

        self.assertEqual(
            record.to_dict()["account_identifiers"]["account_number"],
            "synthetic-account",
        )
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "balances.csv"
            export_balances_csv(
                csv_path, [record], ledger_path=Path(tmp) / "ledger.jsonl"
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(next(csv.DictReader(handle))["account_number"], "synthetic-account")

    def test_balance_export_neutralizes_every_formula_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "balances.csv"

            export_balances_csv(
                csv_path,
                formula_prefix_balance_records(),
                ledger_path=Path(tmp) / "ledger.jsonl",
            )

            with csv_path.open(newline="", encoding="utf-8") as handle:
                balances = [row["balance"] for row in csv.DictReader(handle)]
            self.assertEqual(
                balances,
                ["'=danger", "'+danger", "'-danger", "'@danger", "'\tdanger", "'\rdanger"],
            )

    def test_account_and_holding_constructors_round_trip_through_jsonl(self):
        provenance = Provenance("synthetic", IMPORTED_AT, "fixture")
        account = account_record("brokerage-1", provenance, name="Brokerage", currency="EUR")
        holding = holding_record(account.id, "02.09.2026", "DE000TEST", "3.5", provenance)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")

            ledger.add([account, holding])

            self.assertEqual(ledger.records(), [account, holding])
            self.assertEqual(ledger.records("account"), [account])
            self.assertEqual(ledger.records("holding"), [holding])

    def test_account_and_holding_payload_changes_update_existing_ids(self):
        provenance = Provenance("synthetic", IMPORTED_AT, "fixture")
        later = Provenance(
            "synthetic",
            datetime(2026, 9, 2, 9, 30, tzinfo=timezone.utc),
            "fixture",
        )
        account = account_record("brokerage-1", provenance, name="Old name", currency="EUR")
        renamed = account_record("brokerage-1", later, name="New name", currency="EUR")
        holding = holding_record(account.id, "2026-09-02", "DE000TEST", "3.5", provenance)
        requantified = holding_record(account.id, "2026-09-02", "DE000TEST", "9.0", later)
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            ledger.add([account, holding])

            result = ledger.add([renamed, requantified])

            self.assertEqual((result.added, result.updated, result.duplicates), (0, 2, 0))
            self.assertEqual(ledger.records("account")[0].name, "New name")
            self.assertEqual(ledger.records("holding")[0].quantity, "9.0")

    def test_account_id_joins_balance_using_same_source_account_id(self):
        row = balance_row()
        row["iban"] = "DE00SYNTHETIC"
        row["bank_code"] = ""
        row["account_number"] = ""
        balance = balance_record_from_row(row)
        account = account_record(
            row["iban"],
            Provenance("AqBanking", IMPORTED_AT, "aqbanking:listaccs"),
        )

        self.assertEqual(account.id, balance.account_id)

    def test_legacy_balance_identity_is_readable_and_migrates_before_update(self):
        for identifier_kind in ("fallback", "iban"):
            with self.subTest(identifier_kind=identifier_kind):
                row = balance_row()
                if identifier_kind == "iban":
                    row.update(iban="DE00SYNTHETIC", bank_code="", account_number="")
                    legacy_account_id = stable_id(
                        "acct", row["source"], "iban", row["iban"]
                    )
                else:
                    legacy_account_id = stable_id(
                        "acct",
                        row["source"],
                        "bank_code",
                        row["bank_code"],
                        "account_number",
                        row["account_number"],
                    )
                legacy = balance_record_from_row(row).to_dict()
                legacy["account_id"] = legacy_account_id
                legacy["id"] = stable_id(
                    "bal", row["source"], legacy_account_id, "2026-09-02"
                )
                corrected_row = dict(row)
                corrected_row["balance"] = "145.67 EUR"
                corrected = balance_record_from_row(corrected_row)

                with tempfile.TemporaryDirectory() as tmp:
                    ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
                    ledger.path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

                    self.assertEqual(ledger.records("balance")[0].id, legacy["id"])
                    result = ledger.add([corrected])

                    self.assertEqual((result.added, result.updated), (0, 1))
                    self.assertEqual(ledger.records("balance"), [corrected])

    def test_reimported_complete_transaction_slice_retires_superseded_records(self):
        original_provenance = Provenance("synthetic-csv", IMPORTED_AT, "fixture.csv")
        amended_provenance = Provenance(
            "synthetic-csv",
            datetime(2026, 9, 2, 9, 30, tzinfo=timezone.utc),
            "fixture.csv",
        )
        original = transaction_records(
            [{
                "account_id": "acct_synthetic",
                "booking_date": "2026-09-01",
                "amount": "-10.00 EUR",
                "purpose": "Original purpose",
            }],
            original_provenance,
            complete_source_slice=True,
        )
        amended = transaction_records(
            [{
                "account_id": "acct_synthetic",
                "booking_date": "2026-09-01",
                "amount": "-10.00 EUR",
                "purpose": "Amended purpose",
            }],
            amended_provenance,
            complete_source_slice=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            ledger.add(original)

            result = ledger.replace_transaction_slice(amended, amended_provenance)

            self.assertEqual((result.added, result.removed), (1, 1))
            self.assertEqual(ledger.records("transaction"), amended)

    def test_transaction_slice_rejects_cross_source_ref_identity_collision(self):
        first_provenance = Provenance("synthetic-csv", IMPORTED_AT, "first.csv")
        second_provenance = Provenance("synthetic-csv", IMPORTED_AT, "second.csv")
        rows = [{
            "account_id": "acct_synthetic",
            "booking_date": "2026-09-01",
            "amount": "-10.00 EUR",
            "purpose": "Overlapping transaction",
        }]
        first = transaction_records(rows, first_provenance, complete_source_slice=True)
        second = transaction_records(rows, second_provenance, complete_source_slice=True)

        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            ledger.replace_transaction_slice(first, first_provenance)
            original_bytes = ledger.path.read_bytes()

            with self.assertRaisesRegex(ValueError, "different source slice"):
                ledger.replace_transaction_slice(second, second_provenance)

            self.assertEqual(ledger.path.read_bytes(), original_bytes)
            self.assertEqual(ledger.records("transaction"), first)

    def test_empty_transaction_slice_retires_only_matching_source_records(self):
        first_provenance = Provenance("synthetic-csv", IMPORTED_AT, "first.csv")
        second_provenance = Provenance("synthetic-csv", IMPORTED_AT, "second.csv")
        first = transaction_records(
            [{
                "account_id": "acct_synthetic",
                "booking_date": "2026-09-01",
                "amount": "-10.00 EUR",
                "purpose": "First source",
            }],
            first_provenance,
            complete_source_slice=True,
        )
        second = transaction_records(
            [{
                "account_id": "acct_synthetic",
                "booking_date": "2026-09-01",
                "amount": "-20.00 EUR",
                "purpose": "Second source",
            }],
            second_provenance,
            complete_source_slice=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            ledger.add(first + second)

            result = ledger.replace_transaction_slice([], first_provenance)

            self.assertEqual(result.removed, 1)
            self.assertEqual(ledger.records("transaction"), second)

    def test_generic_transaction_add_does_not_imply_complete_slice(self):
        provenance = Provenance("synthetic-csv", IMPORTED_AT, "fixture.csv")
        records = transaction_records(
            [
                {
                    "account_id": "acct_synthetic",
                    "booking_date": "2026-09-01",
                    "amount": "-10.00 EUR",
                    "purpose": purpose,
                }
                for purpose in ("First", "Second")
            ],
            provenance,
            complete_source_slice=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
            ledger.add(records)

            ledger.add([records[0]])

            self.assertEqual(ledger.records("transaction"), records)

    def test_holding_constructor_rejects_empty_as_of_before_persisting(self):
        provenance = Provenance("synthetic", IMPORTED_AT, "fixture")

        with self.assertRaises(LedgerValidationError) as raised:
            holding_record("acct_synthetic", "", "DE000TEST", "3.5", provenance)

        self.assertEqual([item.field for item in raised.exception.diagnostics], ["as_of"])

    def test_store_rejects_record_that_would_fail_on_read(self):
        provenance = Provenance("synthetic", IMPORTED_AT, "fixture")
        invalid = HoldingRecord(
            id="hold_invalid",
            account_id="acct_synthetic",
            as_of="",
            asset="DE000TEST",
            quantity="3.5",
            provenance=provenance,
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")

            with self.assertRaises(ValueError):
                ledger.add([invalid])

            self.assertFalse(ledger.path.exists())

    def test_jsonl_rejects_malformed_and_forged_records_with_line_number(self):
        provenance = Provenance("synthetic", IMPORTED_AT, "fixture")
        account = account_record("account-1", provenance)
        balance = balance_record_from_row(balance_row())
        transaction = transaction_records(
            [{
                "account_id": account.id,
                "booking_date": "2026-09-02",
                "amount": "-1.00 EUR",
                "purpose": "Coffee",
            }],
            provenance,
            complete_source_slice=True,
        )[0]
        holding = holding_record(account.id, "2026-09-02", "DE000TEST", "3.5", provenance)

        cases: list[tuple[str, object]] = [("top-level shape", None)]
        for label, record in (
            ("account id", account),
            ("balance id", balance),
            ("transaction id", transaction),
            ("holding id", holding),
        ):
            forged = record.to_dict()
            forged["id"] = "forged"
            cases.append((label, forged))
        malformed_provenance = balance.to_dict()
        malformed_provenance["provenance"] = []
        cases.append(("nested provenance shape", malformed_provenance))
        malformed_identifiers = balance.to_dict()
        malformed_identifiers["account_identifiers"] = []
        cases.append(("nested identifier shape", malformed_identifiers))
        forged_account_id = balance.to_dict()
        forged_account_id["account_id"] = "acct_forged"
        cases.append(("balance account id", forged_account_id))
        invalid_date = holding.to_dict()
        invalid_date["as_of"] = "02/09/2026"
        cases.append(("invalid date", invalid_date))
        inconsistent_purpose = transaction.to_dict()
        inconsistent_purpose["normalized_purpose"] = "not coffee"
        cases.append(("normalized purpose", inconsistent_purpose))
        negative_ordinal = transaction.to_dict()
        negative_ordinal["ordinal"] = -1
        cases.append(("negative ordinal", negative_ordinal))
        string_ordinal = transaction.to_dict()
        string_ordinal["ordinal"] = "0"
        cases.append(("ordinal type", string_ordinal))

        for label, value in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")
                ledger.path.write_text(
                    json.dumps(account.to_dict()) + "\n" + json.dumps(value) + "\n",
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, r"ledger\.jsonl:2:"):
                    ledger.records()

    def test_failed_atomic_replace_preserves_existing_ledger_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "ledger.jsonl")
            first = balance_record_from_row(balance_row())
            next_row = balance_row()
            next_row["balance_date"] = "03.09.2026"
            second = balance_record_from_row(next_row)
            ledger.add([first])
            original_bytes = ledger.path.read_bytes()

            with mock.patch(
                "personal_finance_agent.ledger.store.os.replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    ledger.add([second])

            self.assertEqual(ledger.path.read_bytes(), original_bytes)
            self.assertEqual([record.id for record in ledger.records()], [first.id])
            self.assertEqual(list(root.glob(".ledger.jsonl.*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-specific")
    def test_ledger_replace_fsyncs_file_and_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "ledger.jsonl")

            with mock.patch(
                "personal_finance_agent.ledger.store.fsync_parent_directory",
                wraps=fsync_parent_directory,
            ) as fsync_parent:
                ledger.add([balance_record_from_row(balance_row())])

            fsync_parent.assert_any_call(ledger.path)
            fsync_parent.assert_any_call(
                ledger.path.resolve().with_name(".ledger.jsonl.lineage")
            )
            self.assertEqual(fsync_parent.call_count, 2)
            self.assertTrue(ledger.path.exists())

    def test_failed_csv_replace_preserves_existing_export_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "balances.csv"
            ledger_path = root / "ledger.jsonl"
            export_balances_csv(
                csv_path,
                [balance_record_from_row(balance_row())],
                ledger_path=ledger_path,
            )
            original_bytes = csv_path.read_bytes()

            with mock.patch(
                "personal_finance_agent.ledger.export.os.replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    export_balances_csv(
                        csv_path,
                        [balance_record_from_row(balance_row())],
                        ledger_path=ledger_path,
                    )

            self.assertEqual(csv_path.read_bytes(), original_bytes)
            self.assertEqual(list(root.glob(".balances.csv.*.tmp")), [])

    def test_csv_export_rejects_same_path_replacement_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "ledger.jsonl"
            original_ledger = JsonlLedger(ledger_path)
            original_record = balance_record_from_row(balance_row())
            original_ledger.add([original_record])
            csv_path = root / "balances.csv"
            export_balances_csv(csv_path, [original_record], ledger_path=ledger_path)
            original_csv = csv_path.read_bytes()

            replacement_path = root / "replacement.jsonl"
            replacement_ledger = JsonlLedger(replacement_path)
            replacement_row = balance_row()
            replacement_row["balance_date"] = "03.09.2026"
            replacement_record = balance_record_from_row(replacement_row)
            replacement_ledger.add([replacement_record])
            os.replace(replacement_path, ledger_path)

            with self.assertRaisesRegex(ValueError, "not exported by this ledger"):
                export_balances_csv(
                    csv_path, [replacement_record], ledger_path=ledger_path
                )

            self.assertEqual(csv_path.read_bytes(), original_csv)

    def test_csv_export_preserves_lineage_across_normal_ledger_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "ledger.jsonl")
            first = balance_record_from_row(balance_row())
            ledger.add([first])
            csv_path = root / "balances.csv"
            export_balances_csv(csv_path, [first], ledger_path=ledger.path)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                original_marker = next(csv.DictReader(handle))["ledger_export_id"]

            second_row = balance_row()
            second_row["balance_date"] = "03.09.2026"
            second = balance_record_from_row(second_row)
            ledger.add([second])
            export_balances_csv(
                csv_path, ledger.records("balance"), ledger_path=ledger.path
            )

            with csv_path.open(newline="", encoding="utf-8") as handle:
                markers = {row["ledger_export_id"] for row in csv.DictReader(handle)}
            self.assertEqual(markers, {original_marker})

    def test_csv_export_rejects_mixed_row_markers_byte_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "ledger.jsonl")
            first = balance_record_from_row(balance_row())
            second_row = balance_row()
            second_row["balance_date"] = "03.09.2026"
            second = balance_record_from_row(second_row)
            ledger.add([first, second])
            csv_path = root / "balances.csv"
            export_balances_csv(csv_path, [first, second], ledger_path=ledger.path)
            with csv_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
                fieldnames = reader.fieldnames
            rows[1]["ledger_export_id"] = "mentat-ledger-export-v1:different"
            with csv_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            mixed_bytes = csv_path.read_bytes()

            with self.assertRaisesRegex(ValueError, "not exported by this ledger"):
                export_balances_csv(csv_path, [first, second], ledger_path=ledger.path)

            self.assertEqual(csv_path.read_bytes(), mixed_bytes)

    @unittest.skipUnless(os.name == "posix", "requires POSIX process locking")
    def test_concurrent_first_exports_from_different_ledgers_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "balances.csv"
            first_ledger = JsonlLedger(root / "first.jsonl")
            second_ledger = JsonlLedger(root / "second.jsonl")
            first_record = balance_record_from_row(balance_row())
            second_row = balance_row()
            second_row["balance_date"] = "03.09.2026"
            second_record = balance_record_from_row(second_row)
            first_ledger.add([first_record])
            second_ledger.add([second_record])
            context = multiprocessing.get_context("fork")
            first_validated = context.Queue()
            second_validated = context.Queue()
            release_first = context.Event()
            results = context.Queue()
            first_process = context.Process(
                target=_export_after_validation_signal,
                args=(
                    str(csv_path),
                    str(first_ledger.path),
                    first_record,
                    first_validated,
                    release_first,
                    results,
                ),
            )
            second_process = context.Process(
                target=_export_after_validation_signal,
                args=(
                    str(csv_path),
                    str(second_ledger.path),
                    second_record,
                    second_validated,
                    None,
                    results,
                ),
            )
            first_process.start()
            first_validated.get(timeout=5)
            second_process.start()
            try:
                second_validated.get(timeout=0.5)
            except queue.Empty:
                pass
            release_first.set()
            for process in (first_process, second_process):
                process.join(timeout=5)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

            self.assertEqual(
                sorted(results.get(timeout=5) for _ in range(2)),
                ["exported", "rejected"],
            )
            with csv_path.open(newline="", encoding="utf-8") as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(
                [row["balance_date"] for row in exported],
                [first_record.balance_date],
            )

    @unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-specific")
    def test_csv_replace_fsyncs_file_and_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "balances.csv"
            ledger_path = Path(tmp) / "ledger.jsonl"

            with mock.patch(
                "personal_finance_agent.ledger.export.fsync_parent_directory",
                wraps=fsync_parent_directory,
            ) as fsync_parent:
                export_balances_csv(
                    csv_path,
                    [balance_record_from_row(balance_row())],
                    ledger_path=ledger_path,
                )

            fsync_parent.assert_called_once_with(csv_path)
            self.assertTrue(csv_path.exists())

    @unittest.skipUnless(os.name == "posix", "requires POSIX permission bits")
    def test_csv_export_keeps_directory_and_file_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "private" / "balances.csv"

            export_balances_csv(
                csv_path,
                [balance_record_from_row(balance_row())],
                ledger_path=Path(tmp) / "private" / "ledger.jsonl",
            )

            self.assertEqual(stat.S_IMODE(csv_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(csv_path.stat().st_mode), 0o600)
            self.assertEqual(
                stat.S_IMODE(csv_path.with_name(".balances.csv.lock").stat().st_mode),
                0o600,
            )

    @unittest.skipUnless(os.name == "posix", "requires POSIX permission bits")
    def test_atomic_writer_keeps_ledger_directory_and_lock_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "private" / "ledger.jsonl")

            ledger.add([balance_record_from_row(balance_row())])

            self.assertEqual(stat.S_IMODE(ledger.path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(ledger.path.stat().st_mode), 0o600)
            lock_path = ledger.path.with_name(".ledger.jsonl.lock")
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
            lineage_path = ledger.path.with_name(".ledger.jsonl.lineage")
            self.assertEqual(stat.S_IMODE(lineage_path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "requires POSIX advisory file locking")
    def test_concurrent_imports_persist_duplicate_identifier_only_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "ledger.jsonl"
            record = balance_record_from_row(balance_row())
            context = multiprocessing.get_context("fork")
            ready = context.Queue()
            start = context.Event()
            results = context.Queue()
            processes = [
                context.Process(
                    target=_add_record_after_signal,
                    args=(str(ledger_path), record, ready, start, results),
                )
                for _ in range(2)
            ]
            for process in processes:
                process.start()
            for _ in processes:
                ready.get(timeout=5)
            start.set()
            for process in processes:
                process.join(timeout=5)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

            outcomes = sorted(results.get(timeout=5) for _ in processes)
            self.assertEqual(outcomes, [(0, 1), (1, 0)])
            self.assertEqual(
                [stored.id for stored in JsonlLedger(ledger_path).records()],
                [record.id],
            )

    def test_malformed_balance_reports_each_missing_field(self):
        parsed = parse_balance_output("02.09.2026\t123.45 EUR\tsynthetic-account\n", "2026-09-02")

        self.assertEqual(parsed.rows, ())
        self.assertEqual([item.field for item in parsed.diagnostics], ["bank_code", "account_number"])
        self.assertEqual(parsed.diagnostics[0].input_name, "aqbanking:listbal")
        self.assertEqual(parsed.diagnostics[0].line, 1)

    def test_balance_parser_ignores_trailing_blank_lines(self):
        parsed = parse_balance_output(
            "02.09.2026\t123.45 EUR\t\tsynthetic-bank\tsynthetic-account\n\n",
            "2026-09-02",
        )

        self.assertEqual(len(parsed.rows), 1)
        self.assertEqual(parsed.diagnostics, ())


class LedgerCliTests(unittest.TestCase):
    def test_aq_balances_rejects_aliased_ledger_and_csv_path_before_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args([
                "aq",
                "balances",
                "--root",
                str(root),
                "--ledger",
                "state/../shared.data",
                "--csv",
                "shared.data",
            ])

            with mock.patch("personal_finance_agent.cli.base") as command, mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile"
            ) as request:
                with self.assertRaisesRegex(ValueError, "different files"):
                    cmd_aq_balances(args)

            command.assert_not_called()
            request.assert_not_called()
            self.assertFalse((root / "data" / "shared.data").exists())

    def test_aq_balances_allows_case_variant_paths_on_case_sensitive_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args([
                "aq",
                "balances",
                "--root",
                str(root),
                "--ledger",
                "ledger.jsonl",
                "--csv",
                "Ledger.jsonl",
            ])

            with mock.patch(
                "personal_finance_agent.cli._filesystem_is_case_insensitive",
                return_value=False,
                create=True,
            ), mock.patch("personal_finance_agent.cli.base") as command, mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=17
            ) as request:
                self.assertEqual(cmd_aq_balances(args), 17)

            command.assert_called_once()
            request.assert_called_once()

    def test_aq_balances_rejects_missing_case_variant_paths_on_case_insensitive_filesystem(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args([
                "aq",
                "balances",
                "--root",
                str(root),
                "--ledger",
                "ledger.jsonl",
                "--csv",
                "Ledger.jsonl",
            ])

            with mock.patch(
                "personal_finance_agent.cli._filesystem_is_case_insensitive",
                return_value=True,
            ), mock.patch("personal_finance_agent.cli.base") as command, mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile"
            ) as request:
                with self.assertRaisesRegex(ValueError, "different files"):
                    cmd_aq_balances(args)

            command.assert_not_called()
            request.assert_not_called()
            self.assertFalse((root / "data" / "ledger.jsonl").exists())

    def test_aq_balances_refuses_to_overwrite_unmarked_csv_with_ledger_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "data" / "ledger.jsonl")
            ledger.add([balance_record_from_row(balance_row())])
            csv_path = root / "data" / "balances.csv"
            original_bytes = (
                b"date,source,balance_date,balance,iban,bank_code,account_number,exported_at\n"
                b"2026-07-01,AqBanking,2026-07-01,10.00 EUR,,bank,account,2026-07-01T10:00:00+00:00\n"
                b"2026-08-01,AqBanking,2026-08-01,20.00 EUR,,bank,account,2026-08-01T10:00:00+00:00\n"
            )
            csv_path.write_bytes(original_bytes)
            args = build_parser().parse_args(["aq", "balances", "--root", str(root)])

            with mock.patch("personal_finance_agent.cli.base") as command, mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile"
            ) as request:
                with self.assertRaisesRegex(ValueError, "existing CSV"):
                    cmd_aq_balances(args)

            command.assert_not_called()
            request.assert_not_called()
            self.assertEqual(csv_path.read_bytes(), original_bytes)

    def test_ledger_export_refuses_csv_produced_by_different_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = balance_record_from_row(balance_row())
            first_ledger = root / "data" / "first.jsonl"
            second_ledger = JsonlLedger(root / "data" / "second.jsonl")
            second_ledger.add([record])
            csv_path = root / "data" / "balances.csv"
            export_balances_csv(csv_path, [record], ledger_path=first_ledger)
            original_bytes = csv_path.read_bytes()
            args = build_parser().parse_args([
                "ledger",
                "export",
                "--root",
                str(root),
                "--ledger",
                "second.jsonl",
            ])

            with self.assertRaisesRegex(ValueError, "not exported by this ledger"):
                cmd_ledger_export(args)

            self.assertEqual(csv_path.read_bytes(), original_bytes)

    def test_ledger_export_rejects_empty_ledger_without_creating_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "data" / "balances.csv"
            args = build_parser().parse_args(["ledger", "export", "--root", str(root)])

            with self.assertRaisesRegex(ValueError, "no balance records"):
                cmd_ledger_export(args)

            self.assertFalse(csv_path.exists())

    def test_aq_balances_empty_input_preserves_existing_csv_and_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "data" / "ledger.jsonl")
            ledger.add([balance_record_from_row(balance_row())])
            csv_path = root / "data" / "balances.csv"
            export_balances_csv(
                csv_path, ledger.records("balance"), ledger_path=ledger.path
            )
            original_bytes = csv_path.read_bytes()
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )
            errors = io.StringIO()

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch("personal_finance_agent.cli.run_capture", return_value=""), redirect_stderr(errors):
                self.assertEqual(cmd_aq_balances(args), 2)

            self.assertIn("no valid balance records", errors.getvalue())
            self.assertEqual(csv_path.read_bytes(), original_bytes)

    def test_aq_balances_fully_rejected_input_preserves_existing_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "data" / "ledger.jsonl")
            ledger.add([balance_record_from_row(balance_row())])
            csv_path = root / "data" / "balances.csv"
            export_balances_csv(
                csv_path, ledger.records("balance"), ledger_path=ledger.path
            )
            original_bytes = csv_path.read_bytes()
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch(
                "personal_finance_agent.cli.run_capture",
                return_value="02.09.2026\t99.00 EUR\tsynthetic-account\n",
            ), mock.patch(
                "personal_finance_agent.cli.export_balances_csv",
                wraps=export_balances_csv,
            ) as export, redirect_stderr(io.StringIO()):
                self.assertEqual(cmd_aq_balances(args), 2)

            export.assert_not_called()
            self.assertEqual(csv_path.read_bytes(), original_bytes)

    def test_ledger_export_rejects_aliased_ledger_and_csv_path_before_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args([
                "ledger",
                "export",
                "--root",
                str(root),
                "--ledger",
                "state/../shared.data",
                "--csv",
                "shared.data",
            ])

            with mock.patch("personal_finance_agent.cli.export_balances_csv") as export:
                with self.assertRaisesRegex(ValueError, "different files"):
                    cmd_ledger_export(args)

            export.assert_not_called()
            self.assertFalse((root / "data" / "shared.data").exists())

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unsupported")
    def test_ledger_export_rejects_hardlinked_ledger_and_csv_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            data_dir.mkdir()
            ledger_path = data_dir / "ledger.jsonl"
            csv_path = data_dir / "balances.csv"
            ledger_path.write_text("synthetic ledger placeholder\n", encoding="utf-8")
            os.link(ledger_path, csv_path)
            args = build_parser().parse_args(["ledger", "export", "--root", str(root)])

            with mock.patch("personal_finance_agent.cli.export_balances_csv") as export:
                with self.assertRaisesRegex(ValueError, "different files"):
                    cmd_ledger_export(args)

            export.assert_not_called()
            self.assertEqual(ledger_path.read_bytes(), csv_path.read_bytes())

    def test_ledger_export_refuses_existing_csv_without_ledger_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "data" / "balances.csv"
            csv_path.parent.mkdir(parents=True)
            original_bytes = b"legacy,balance,history\n"
            csv_path.write_bytes(original_bytes)
            args = build_parser().parse_args(["ledger", "export", "--root", str(root)])

            with self.assertRaisesRegex(ValueError, "existing CSV"):
                cmd_ledger_export(args)

            self.assertEqual(csv_path.read_bytes(), original_bytes)

    def test_aq_balances_reports_malformed_sibling_and_imports_valid_row(self):
        output = (
            "02.09.2026\t123.45 EUR\t\tsynthetic-bank\tsynthetic-account\n"
            "02.09.2026\t99.00 EUR\tsynthetic-account\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )
            errors = io.StringIO()

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch("personal_finance_agent.cli.run_capture", return_value=output), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(errors):
                self.assertEqual(cmd_aq_balances(args), 2)

            diagnostics = [json.loads(line)["diagnostic"] for line in errors.getvalue().splitlines()]
            self.assertEqual([item["field"] for item in diagnostics], ["bank_code", "account_number"])
            self.assertEqual(diagnostics[0]["line"], 2)
            self.assertEqual(len(JsonlLedger(root / "data" / "ledger.jsonl").records("balance")), 1)

    def test_aq_balances_twice_keeps_one_stable_ledger_record(self):
        output = "02.09.2026\t-123.45 EUR\t\tsynthetic-bank\tsynthetic-account\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch("personal_finance_agent.cli.run_capture", return_value=output), mock.patch(
                "builtins.print"
            ):
                self.assertEqual(cmd_aq_balances(args), 0)
                first_bytes = (root / "data" / "ledger.jsonl").read_bytes()
                self.assertEqual(cmd_aq_balances(args), 0)

            ledger_path = root / "data" / "ledger.jsonl"
            records = JsonlLedger(ledger_path).records("balance")
            self.assertEqual(len(records), 1)
            self.assertEqual(ledger_path.read_bytes(), first_bytes)
            self.assertEqual(records[0].amount, "-123.45 EUR")

            with (root / "data" / "balances.csv").open(newline="", encoding="utf-8") as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0]["balance"], "'-123.45 EUR")

    def test_aq_balances_summary_reports_updated_records(self):
        original = "02.09.2026\t123.45 EUR\t\tsynthetic-bank\tsynthetic-account\n"
        corrected = "02.09.2026\t145.67 EUR\t\tsynthetic-bank\tsynthetic-account\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )
            output = io.StringIO()

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch(
                "personal_finance_agent.cli.run_capture", side_effect=[original, corrected]
            ), redirect_stdout(output), redirect_stderr(io.StringIO()):
                self.assertEqual(cmd_aq_balances(args), 0)
                self.assertEqual(cmd_aq_balances(args), 0)

            self.assertIn(
                "Ledger import: 0 added, 1 updated, 0 duplicate(s)",
                output.getvalue(),
            )

    def test_aq_balances_reports_balance_amount_change_as_diagnostic(self):
        original = "02.09.2026\t123.45 EUR\t\tsynthetic-bank\tsynthetic-account\n"
        corrected = "02.09.2026\t145.67 EUR\t\tsynthetic-bank\tsynthetic-account\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )
            errors = io.StringIO()

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch(
                "personal_finance_agent.cli.run_capture", side_effect=[original, corrected]
            ), redirect_stdout(io.StringIO()), redirect_stderr(errors):
                self.assertEqual(cmd_aq_balances(args), 0)
                self.assertEqual(cmd_aq_balances(args), 0)

            diagnostic = json.loads(errors.getvalue().splitlines()[-1])["diagnostic"]
            self.assertEqual(diagnostic["field"], "amount")
            self.assertEqual(diagnostic["previous_value"], "123.45 EUR")
            self.assertEqual(diagnostic["new_value"], "145.67 EUR")
            self.assertTrue(diagnostic["record_id"].startswith("bal_"))

    def test_semantic_balance_diagnostic_includes_source_line(self):
        output = (
            "02.09.2026\t123.45 EUR\t\tsynthetic-bank\tsynthetic-account\n"
            "31.02.2026\t99.00 EUR\t\tsynthetic-bank\tsynthetic-account\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )
            errors = io.StringIO()

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch("personal_finance_agent.cli.run_capture", return_value=output), redirect_stdout(
                io.StringIO()
            ), redirect_stderr(errors):
                self.assertEqual(cmd_aq_balances(args), 2)

            diagnostics = [json.loads(line)["diagnostic"] for line in errors.getvalue().splitlines()]
            self.assertEqual(diagnostics[-1]["field"], "balance_date")
            self.assertEqual(diagnostics[-1]["line"], 2)

    @unittest.skipUnless(os.name == "posix", "requires POSIX advisory file locking")
    def test_aq_balances_holds_ledger_lock_through_csv_publication(self):
        output = "02.09.2026\t123.45 EUR\t\tsynthetic-bank\tsynthetic-account\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "data" / "ledger.jsonl"
            args = build_parser().parse_args(
                ["aq", "balances", "--root", str(root), "--date", "2026-09-02"]
            )
            actual_export = export_balances_csv

            def assert_locked(csv_path, records, *, ledger_path):
                process_context = multiprocessing.get_context("fork")
                results = process_context.Queue()
                process = process_context.Process(
                    target=_try_nonblocking_ledger_lock,
                    args=(str(ledger_path), results),
                )
                process.start()
                process.join(timeout=5)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
                self.assertFalse(results.get(timeout=5))
                return actual_export(csv_path, records, ledger_path=ledger_path)

            with mock.patch("personal_finance_agent.cli.base", return_value=["synthetic-tool"]), mock.patch(
                "personal_finance_agent.cli.run_with_optional_pinfile", return_value=0
            ), mock.patch("personal_finance_agent.cli.run_capture", return_value=output), mock.patch(
                "personal_finance_agent.cli.export_balances_csv", side_effect=assert_locked
            ), mock.patch("builtins.print"):
                self.assertEqual(cmd_aq_balances(args), 0)

    @unittest.skipUnless(os.name == "posix", "requires POSIX advisory file locking")
    def test_cli_export_holds_ledger_lock_through_csv_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "data" / "ledger.jsonl"
            JsonlLedger(ledger_path).add([balance_record_from_row(balance_row())])
            args = build_parser().parse_args(["ledger", "export", "--root", str(root)])
            actual_export = export_balances_csv

            def assert_locked(csv_path, records, *, ledger_path):
                process_context = multiprocessing.get_context("fork")
                results = process_context.Queue()
                process = process_context.Process(
                    target=_try_nonblocking_ledger_lock,
                    args=(str(ledger_path), results),
                )
                process.start()
                process.join(timeout=5)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
                self.assertFalse(results.get(timeout=5))
                return actual_export(csv_path, records, ledger_path=ledger_path)

            with mock.patch(
                "personal_finance_agent.cli.export_balances_csv", side_effect=assert_locked
            ), mock.patch("builtins.print"):
                self.assertEqual(cmd_ledger_export(args), 0)
    def test_cli_export_reads_existing_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "data" / "ledger.jsonl")
            ledger.add([balance_record_from_row(balance_row())])
            args = build_parser().parse_args(
                ["ledger", "export", "--root", str(root), "--csv", "exports/balances.csv"]
            )

            with mock.patch(
                "personal_finance_agent.cli.parse_balance_output",
                side_effect=AssertionError("export must not invoke a connector parser"),
            ), mock.patch("builtins.print"):
                self.assertEqual(cmd_ledger_export(args), 0)

            with (root / "data" / "exports" / "balances.csv").open(newline="", encoding="utf-8") as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(len(exported), 1)
            self.assertEqual(exported[0]["balance"], "123.45 EUR")
            self.assertTrue(
                exported[0]["ledger_export_id"].startswith("mentat-ledger-export-v1:")
            )

    def test_cli_export_neutralizes_every_formula_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "data" / "ledger.jsonl")
            ledger.add(formula_prefix_balance_records())
            args = build_parser().parse_args(["ledger", "export", "--root", str(root)])

            with mock.patch("builtins.print"):
                self.assertEqual(cmd_ledger_export(args), 0)

            with (root / "data" / "balances.csv").open(newline="", encoding="utf-8") as handle:
                balances = [row["balance"] for row in csv.DictReader(handle)]
            self.assertEqual(
                balances,
                ["'=danger", "'+danger", "'-danger", "'@danger", "'\tdanger", "'\rdanger"],
            )

    def test_cli_inspection_emits_machine_readable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = JsonlLedger(root / "data" / "ledger.jsonl")
            ledger.add([balance_record_from_row(balance_row())])
            args = build_parser().parse_args(["ledger", "inspect", "--root", str(root), "--json"])

            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(cmd_ledger_inspect(args), 0)

            payload = json.loads(output.getvalue())
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["records"][0]["record_type"], "balance")


if __name__ == "__main__":
    unittest.main()
