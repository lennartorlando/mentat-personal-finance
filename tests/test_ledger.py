import csv
import io
import json
import multiprocessing
import os
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
    JsonlLedger,
    LedgerValidationError,
    Provenance,
    balance_record_from_row,
    export_balances_csv,
    transaction_records,
)


IMPORTED_AT = datetime(2026, 9, 2, 8, 30, tzinfo=timezone.utc)


def _add_record_after_signal(
    ledger_path: str, record, ready, start, results
) -> None:
    ready.put(True)
    start.wait()
    result = JsonlLedger(Path(ledger_path)).add([record])
    results.put((result.added, result.duplicates))


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

    def test_failed_csv_replace_preserves_existing_export_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "balances.csv"
            original_bytes = b"previous,export\nleave,this intact\n"
            csv_path.write_bytes(original_bytes)

            with mock.patch(
                "personal_finance_agent.ledger.export.os.replace",
                side_effect=OSError("synthetic replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "synthetic replace failure"):
                    export_balances_csv(csv_path, [balance_record_from_row(balance_row())])

            self.assertEqual(csv_path.read_bytes(), original_bytes)
            self.assertEqual(list(root.glob(".balances.csv.*.tmp")), [])

    @unittest.skipUnless(os.name == "posix", "requires POSIX permission bits")
    def test_csv_export_keeps_directory_and_file_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "private" / "balances.csv"

            export_balances_csv(csv_path, [balance_record_from_row(balance_row())])

            self.assertEqual(stat.S_IMODE(csv_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(csv_path.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "requires POSIX permission bits")
    def test_atomic_writer_keeps_ledger_directory_and_lock_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlLedger(Path(tmp) / "private" / "ledger.jsonl")

            ledger.add([balance_record_from_row(balance_row())])

            self.assertEqual(stat.S_IMODE(ledger.path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(ledger.path.stat().st_mode), 0o600)
            lock_path = ledger.path.with_name(".ledger.jsonl.lock")
            self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)

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

        self.assertEqual(list(parsed), [])
        self.assertEqual([item.field for item in parsed.diagnostics], ["bank_code", "account_number"])
        self.assertEqual(parsed.diagnostics[0].input_name, "aqbanking:listbal")
        self.assertEqual(parsed.diagnostics[0].line, 1)


class LedgerCliTests(unittest.TestCase):
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
                exported = next(csv.DictReader(handle))
            self.assertEqual(exported["balance"], "'-123.45 EUR")

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
