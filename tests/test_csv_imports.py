from __future__ import annotations

import json
import io
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from personal_finance_agent.cli import main
from personal_finance_agent.connectors.csv_import import parse_csv
from personal_finance_agent.connectors.csv_profile import (
    CsvProfileError,
    load_csv_profile,
    normalize_source_account_id,
)
from personal_finance_agent.ledger import JsonlLedger, balance_record_from_row


FIXTURES = Path(__file__).parent / "fixtures" / "imports"


def profile_value(amount=None, identifier_kind="opaque", identifier="BrokerRef-AbC"):
    return {
        "source": "Synthetic Broker",
        "source_ref": "synthetic-broker-primary",
        "account": {
            "identifier": identifier,
            "identifier_kind": identifier_kind,
            "name": "Synthetic Depot",
            "currency": "EUR",
        },
        "period": {"start": "2026-01-01", "end": "2026-01-31"},
        "format": {
            "encoding": "utf-8",
            "delimiter": ";",
            "quotechar": '"',
            "decimal_mark": ",",
            "thousands_separator": ".",
            "date_format": "%d.%m.%Y",
        },
        "columns": {
            "booking_date": "Datum",
            "purpose": ["Text", "Notiz"],
            "denomination": "Waehrung",
        },
        "amount": amount or {"strategy": "signed", "column": "Betrag"},
    }


def write_profile(directory, value):
    path = Path(directory) / "profile.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return load_csv_profile(path)


def write_profile_json(directory, value, name="profile.json"):
    path = Path(directory) / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def write_csv(directory, rows, name="input.csv", encoding="utf-8"):
    path = Path(directory) / name
    path.write_text(
        "Datum;Text;Notiz;Betrag;Waehrung\n" + "\n".join(rows) + "\n",
        encoding=encoding,
    )
    return path


def run_cli(*arguments):
    output = io.StringIO()
    errors = io.StringIO()
    with mock.patch("sys.argv", ["personal-finance-agent", *map(str, arguments)]), redirect_stdout(
        output
    ), redirect_stderr(errors):
        code = main()
    return code, output.getvalue(), errors.getvalue()


class CsvProfileTests(unittest.TestCase):
    def test_profile_requires_every_declared_file_shape_property(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            del value["format"]["quotechar"]
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(CsvProfileError) as raised:
                load_csv_profile(path)

            self.assertEqual(raised.exception.diagnostics[0].field, "format.quotechar")

    def test_account_identifier_normalisation_is_kind_scoped(self):
        self.assertEqual(
            normalize_source_account_id("DE00 SYNTH 0000 0000 0000 00", "iban"),
            normalize_source_account_id("de00synth00000000000000", "iban"),
        )
        self.assertEqual(normalize_source_account_id("  AbC-19x  ", "opaque"), "AbC-19x")

    def test_period_is_declared_and_validated_without_deriving_from_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["period"] = {"start": "2026-02-01", "end": "2026-01-31"}
            path = Path(tmp) / "profile.json"
            path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(CsvProfileError) as raised:
                load_csv_profile(path)

            self.assertEqual(raised.exception.diagnostics[0].field, "period")


class CsvFormatTests(unittest.TestCase):
    def test_semicolon_iso_8859_1_decimal_comma_and_metadata_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["format"]["encoding"] = "iso-8859-1"
            profile = write_profile(tmp, value)

            result = parse_csv(FIXTURES / "dach_semicolon_iso_8859_1.csv", profile)

            self.assertEqual(result.diagnostics, ())
            self.assertEqual(
                result.rows,
                ({
                    "booking_date": "2026-01-12",
                    "amount": "1234.56 EUR",
                    "purpose": "Synthetic purchase | fuer Uebung",
                },),
            )

    def test_each_declared_amount_strategy_normalises_to_same_amount(self):
        cases = (
            ({"strategy": "signed", "column": "Betrag"}, "Betrag;Waehrung\n-1.234,560;EUR\n", "-1234.560 EUR"),
            ({"strategy": "inverted", "column": "Betrag"}, "Betrag;Waehrung\n1.234,560;EUR\n", "-1234.560 EUR"),
            (
                {"strategy": "debit_credit_columns", "debit_column": "Soll", "credit_column": "Haben"},
                "Soll;Haben;Waehrung\n1.234,560;;EUR\n",
                "-1234.560 EUR",
            ),
            (
                {"strategy": "debit_credit_columns", "debit_column": "Soll", "credit_column": "Haben"},
                "Soll;Haben;Waehrung\n;1.234,560;EUR\n",
                "1234.560 EUR",
            ),
            (
                {
                    "strategy": "indicator",
                    "column": "Betrag",
                    "indicator_column": "Richtung",
                    "debit_values": ["S"],
                    "credit_values": ["H"],
                },
                "Betrag;Richtung;Waehrung\n1.234,560;S;EUR\n",
                "-1234.560 EUR",
            ),
            (
                {
                    "strategy": "indicator",
                    "column": "Betrag",
                    "indicator_column": "Richtung",
                    "debit_values": ["S"],
                    "credit_values": ["H"],
                },
                "Betrag;Richtung;Waehrung\n1.234,560;H;EUR\n",
                "1234.560 EUR",
            ),
        )
        for amount, amount_columns, expected in cases:
            with self.subTest(strategy=amount["strategy"]), tempfile.TemporaryDirectory() as tmp:
                value = profile_value(amount)
                value["columns"]["purpose"] = ["Text"]
                profile = write_profile(tmp, value)
                csv_path = Path(tmp) / "input.csv"
                csv_path.write_text(
                    "Datum;Text;" + amount_columns.split("\n", 1)[0] + "\n"
                    "12.01.2026;Synthetic;" + amount_columns.split("\n", 1)[1],
                    encoding="utf-8",
                )

                result = parse_csv(csv_path, profile)

                self.assertEqual(result.diagnostics, ())
                self.assertEqual(result.rows[0]["amount"], expected)

    def test_crypto_amount_keeps_eight_significant_decimals(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["format"].update(decimal_mark=".", thousands_separator=",")
            value["columns"]["purpose"] = ["Text"]
            profile = write_profile(tmp, value)
            path = Path(tmp) / "crypto.csv"
            path.write_text(
                "Datum;Text;Betrag;Waehrung\n12.01.2026;Synthetic;0.00000001;BTC\n",
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            self.assertEqual(result.rows[0]["amount"], "0.00000001 BTC")

    def test_utf8_bom_does_not_hide_first_header_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["columns"]["purpose"] = ["Text"]
            profile = write_profile(tmp, value)
            path = Path(tmp) / "bom.csv"
            path.write_text(
                "\ufeffDatum;Text;Betrag;Waehrung\n12.01.2026;Synthetic;1,00;EUR\n",
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            self.assertEqual(result.diagnostics, ())
            self.assertEqual(result.rows[0]["booking_date"], "2026-01-12")

    def test_quoted_purpose_preserves_delimiter_and_embedded_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["columns"]["purpose"] = ["Text"]
            profile = write_profile(tmp, value)
            path = Path(tmp) / "multiline.csv"
            path.write_text(
                'Datum;Text;Betrag;Waehrung\n12.01.2026;"Synthetic; note\ncontinued";1,00;EUR\n',
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            self.assertEqual(result.rows[0]["purpose"], "Synthetic; note\ncontinued")

    def test_empty_declared_purpose_is_a_value_free_row_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = write_profile(tmp, profile_value())
            path = Path(tmp) / "empty-purpose.csv"
            path.write_text(
                "Datum;Text;Notiz;Betrag;Waehrung\n12.01.2026;;;77,44;EUR\n",
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            self.assertEqual(result.rows, ())
            self.assertEqual(result.diagnostics[0].field, "purpose")
            self.assertIn("columns.purpose", result.diagnostics[0].message)
            self.assertEqual(result.diagnostics[0].line, 2)
            self.assertNotIn("77,44", str(result.diagnostics))

    def test_AE6_wrong_profile_names_key_and_missing_column_before_data_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = write_profile(tmp, profile_value())
            path = Path(tmp) / "wrong-profile.csv"
            path.write_bytes(
                b"Datum;Text;Betrag;Waehrung\n"
                b"\xff invalid data that must never be decoded\n"
            )

            result = parse_csv(path, profile)

            self.assertEqual(result.rows, ())
            diagnostic_text = str(result.diagnostics)
            self.assertIn("columns.purpose[1]", diagnostic_text)
            self.assertIn("Notiz", diagnostic_text)
            self.assertNotIn("invalid data", diagnostic_text)

    def test_no_header_diagnostic_describes_candidates_without_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = write_profile(tmp, profile_value())
            path = Path(tmp) / "secret-metadata.csv"
            path.write_text("Secret Holder 19;DE00SECRET\n123;abc\n", encoding="utf-8")

            result = parse_csv(path, profile)

            message = result.diagnostics[0].message
            self.assertIn("2 column(s)", message)
            self.assertIn("letters", message)
            self.assertNotIn("Secret", message)
            self.assertNotIn("DE00SECRET", message)

    def test_header_diagnostic_strips_ansi_and_control_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = write_profile(tmp, profile_value())
            path = Path(tmp) / "header.csv"
            path.write_text(
                "Datum;Text;Betrag;Waehrung;\x1b[31mExtraHeader\x1b[0m" + "x" * 100 + "\n"
                "12.01.2026;Synthetic;1,00;EUR;x\n",
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            diagnostic_text = str(result.diagnostics)
            self.assertIn("ExtraHeader", diagnostic_text)
            self.assertNotIn("\x1b", diagnostic_text)
            self.assertNotIn("x" * 81, diagnostic_text)

    def test_partial_header_match_never_echoes_unconfirmed_csv_cells(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = write_profile(tmp, profile_value())
            path = Path(tmp) / "preamble.csv"
            path.write_text(
                "Datum;Text;SENSITIVE_HOLDER;SENSITIVE_ACCOUNT\n"
                "Datum;Text;Notiz;Betrag;Waehrung\n"
                "12.01.2026;Synthetic;Note;1,00;EUR\n",
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            self.assertEqual(result.diagnostics, ())
            self.assertEqual(len(result.rows), 1)
            self.assertNotIn("SENSITIVE_HOLDER", str(result))
            self.assertNotIn("SENSITIVE_ACCOUNT", str(result))

    def test_unparseable_date_reports_position_and_format_without_cell(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["columns"]["purpose"] = ["Text"]
            profile = write_profile(tmp, value)
            path = Path(tmp) / "date.csv"
            path.write_text(
                "Datum;Text;Betrag;Waehrung\nNOT_A_DATE;Synthetic;1,00;EUR\n",
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            diagnostic = result.diagnostics[0]
            self.assertEqual((diagnostic.field, diagnostic.line), ("booking_date", 2))
            self.assertIn("columns.booking_date", diagnostic.message)
            self.assertIn("%d.%m.%Y", diagnostic.message)
            self.assertIn("column 1", diagnostic.message)
            self.assertNotIn("NOT_A_DATE", str(diagnostic))

    def test_ragged_row_returns_diagnostic_instead_of_raising_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["columns"]["purpose"] = ["Text"]
            profile = write_profile(tmp, value)
            path = Path(tmp) / "ragged.csv"
            path.write_text(
                "Datum;Text;Betrag;Waehrung\n12.01.2026;Synthetic;1,00\n",
                encoding="utf-8",
            )

            result = parse_csv(path, profile)

            self.assertEqual(result.diagnostics[0].field, "record")
            self.assertIn("line has 3 column(s); header has 4", result.diagnostics[0].message)

    def test_mid_file_decode_failure_reports_line_without_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            value = profile_value()
            value["columns"]["purpose"] = ["Text"]
            profile = write_profile(tmp, value)
            path = Path(tmp) / "decode.csv"
            path.write_bytes(
                b"Datum;Text;Betrag;Waehrung\n12.01.2026;Synthetic;1,00;EUR\n\xff;bad\n"
            )

            result = parse_csv(path, profile)

            diagnostic = result.diagnostics[0]
            self.assertEqual((diagnostic.field, diagnostic.line), ("encoding", 3))
            self.assertNotIn("bad", str(diagnostic))

    def test_nul_byte_file_is_refused_as_non_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = write_profile(tmp, profile_value())
            path = Path(tmp) / "nul.csv"
            path.write_bytes(b"Datum;Text;Notiz;Betrag;Waehrung\x00\n")

            result = parse_csv(path, profile)

            self.assertEqual(result.rows, ())
            self.assertEqual(result.diagnostics[0].field, "input")
            self.assertIn("not text", result.diagnostics[0].message)


class CsvImportCommandTests(unittest.TestCase):
    def test_AE1_first_import_writes_account_and_transactions_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = profile_value()
            value["format"]["encoding"] = "iso-8859-1"
            profile = write_profile_json(root, value)
            source = write_csv(root, ["12.01.2026;Kauf;fuer Uebung;1.234,56;EUR"], encoding="iso-8859-1")

            code, output, errors = run_cli(
                "ledger", "import-csv", "--root", root, "--input", source, "--profile", profile
            )

            records = JsonlLedger(root / "data" / "ledger.jsonl").records()
            self.assertEqual(code, 0, errors)
            self.assertIn("1 added", output)
            self.assertEqual([record.record_type for record in records], ["account", "transaction"])
            self.assertEqual(records[1].provenance.source_ref, "synthetic-broker-primary")

    def test_AE2_exact_reimport_is_duplicate_and_ledger_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            source = write_csv(root, ["12.01.2026;Kauf;Notiz;1,00;EUR"])
            arguments = ("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)
            self.assertEqual(run_cli(*arguments)[0], 0)
            ledger_path = root / "data" / "ledger.jsonl"
            before = ledger_path.read_bytes()

            code, output, errors = run_cli(*arguments)

            self.assertEqual(code, 0, errors)
            self.assertIn("1 duplicate", output)
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_AE3_overlapping_reimport_preserves_outside_period_and_supersedes_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = profile_value()
            value["period"] = {"start": "2026-01-01", "end": "2026-06-30"}
            profile = write_profile_json(root, value)
            source = write_csv(
                root,
                ["12.01.2026;January;Old;1,00;EUR", "12.04.2026;April;Old;2,00;EUR"],
            )
            arguments = ("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)
            self.assertEqual(run_cli(*arguments)[0], 0)
            source.write_text(
                "Datum;Text;Notiz;Betrag;Waehrung\n12.04.2026;April;Corrected;2,00;EUR\n"
                "12.09.2026;September;New;3,00;EUR\n",
                encoding="utf-8",
            )

            code, _, errors = run_cli(*arguments, "--period", "2026-03-01", "2026-09-30")

            transactions = JsonlLedger(root / "data" / "ledger.jsonl").records("transaction")
            self.assertEqual(code, 0, errors)
            self.assertEqual(
                [(record.booking_date, record.purpose) for record in transactions],
                [("2026-01-12", "January | Old"), ("2026-04-12", "April | Corrected"), ("2026-09-12", "September | New")],
            )

    def test_AE4_corrected_export_reports_one_addition_and_one_retirement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            source = write_csv(root, ["12.01.2026;Old purpose;Note;1,00;EUR"])
            arguments = ("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)
            self.assertEqual(run_cli(*arguments)[0], 0)
            source = write_csv(root, ["12.01.2026;Corrected purpose;Note;1,00;EUR"])

            code, output, errors = run_cli(*arguments)

            self.assertEqual(code, 0, errors)
            self.assertIn("1 added", output)
            self.assertIn("1 retired", output)

    def test_AE5_malformed_row_reports_all_errors_without_values_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            source = write_csv(
                root,
                ["SECRET_DATE;First;Note;1,00;EUR", "ALSO_SECRET;Second;Note;2,00;EUR"],
            )

            code, _, errors = run_cli(
                "ledger", "import-csv", "--root", root, "--input", source, "--profile", profile
            )

            self.assertEqual(code, 2)
            self.assertFalse((root / "data" / "ledger.jsonl").exists())
            self.assertIn('"line": 2', errors)
            self.assertIn('"line": 3', errors)
            self.assertIn("%d.%m.%Y", errors)
            self.assertNotIn("SECRET_DATE", errors)
            self.assertNotIn("ALSO_SECRET", errors)

    def test_AE7_imported_transactions_and_fints_balances_coexist_in_inspect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_path = root / "data" / "ledger.jsonl"
            ledger = JsonlLedger(ledger_path)
            balance = balance_record_from_row(
                {
                    "date": "2026-01-31", "source": "AqBanking", "balance_date": "2026-01-31",
                    "balance": "50.00 EUR", "iban": "DE00SYNTH00000000000000", "bank_code": "",
                    "account_number": "", "exported_at": "2026-01-31T12:00:00+00:00",
                }
            )
            ledger.add([balance])
            profile = write_profile_json(root, profile_value())
            source = write_csv(root, ["12.01.2026;Kauf;Notiz;1,00;EUR"])
            self.assertEqual(run_cli("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)[0], 0)

            code, output, errors = run_cli("ledger", "inspect", "--root", root, "--json")

            value = json.loads(output)
            self.assertEqual(code, 0, errors)
            self.assertEqual({record["record_type"] for record in value["records"]}, {"account", "balance", "transaction"})

    def test_AE8_preview_renders_rows_and_period_without_opening_or_creating_ledger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            source = write_csv(root, ["12.01.2026;Kauf;Notiz;1,00;EUR"])
            with mock.patch("personal_finance_agent.cli.JsonlLedger", side_effect=AssertionError("ledger opened")):
                code, output, errors = run_cli(
                    "ledger", "import-csv", "--root", root, "--input", source, "--profile", profile, "--preview"
                )

            value = json.loads(output)
            self.assertEqual(code, 0, errors)
            self.assertEqual(value["period"], {"start": "2026-01-01", "end": "2026-01-31"})
            self.assertEqual(value["rows"][0]["amount"], "1.00 EUR")
            self.assertFalse((root / "data" / "ledger.jsonl").exists())

    def test_KTD4_regression_overlapping_stable_slice_keeps_outside_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            source = write_csv(
                root,
                [
                    "12.01.2026;Keep;January;1,00;EUR",
                    "12.04.2026;Replace;April;2,00;EUR",
                    "12.05.2026;Withdrawn;May;3,00;EUR",
                ],
            )
            base_args = ("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)
            self.assertEqual(run_cli(*base_args, "--period", "2026-01-01", "2026-06-30")[0], 0)
            source = write_csv(root, ["12.04.2026;Replacement;April;2,00;EUR"])

            code, output, errors = run_cli(
                *base_args, "--period", "2026-03-01", "2026-09-30"
            )

            transactions = JsonlLedger(root / "data" / "ledger.jsonl").records("transaction")
            self.assertEqual(code, 0, errors)
            self.assertIn("1 added", output)
            self.assertIn("2 retired", output)
            self.assertEqual([record.booking_date for record in transactions], ["2026-01-12", "2026-04-12"])
            self.assertEqual({record.provenance.source_ref for record in transactions}, {"synthetic-broker-primary"})

    def test_KTD5_regression_partial_parse_cannot_delete_existing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            source = write_csv(
                root,
                [
                    "12.01.2026;First;Note;1,00;EUR",
                    "13.01.2026;Second;Note;2,00;EUR",
                    "14.01.2026;Third;Note;3,00;EUR",
                ],
            )
            arguments = ("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)
            self.assertEqual(run_cli(*arguments)[0], 0)
            ledger_path = root / "data" / "ledger.jsonl"
            before = ledger_path.read_bytes()
            source = write_csv(
                root,
                [
                    "12.01.2026;First;Note;1,00;EUR",
                    "BAD_DATE;Second;Note;2,00;EUR",
                    "14.01.2026;Third;Note;3,00;EUR",
                ],
            )

            code, _, _ = run_cli(*arguments)

            self.assertEqual(code, 2)
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_changed_source_ref_collision_leaves_ledger_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = profile_value()
            profile = write_profile_json(root, value)
            source = write_csv(root, ["12.01.2026;First;Note;1,00;EUR"])
            arguments = ("ledger", "import-csv", "--root", root, "--input", source)
            self.assertEqual(run_cli(*arguments, "--profile", profile)[0], 0)
            ledger_path = root / "data" / "ledger.jsonl"
            before = ledger_path.read_bytes()
            value["source_ref"] = "synthetic-broker-renamed-slice"
            changed_profile = write_profile_json(root, value, "changed.local.json")

            code, _, errors = run_cli(*arguments, "--profile", changed_profile)

            self.assertEqual(code, 2)
            self.assertIn("different source slice", errors)
            self.assertEqual(ledger_path.read_bytes(), before)

    def test_unsafe_sources_are_refused_on_resolved_paths_before_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            ledger = data / "ledger.jsonl"
            ledger.write_text("secret ledger", encoding="utf-8")
            profile = write_profile_json(root, profile_value())
            aliases = root / "aliases"
            aliases.mkdir()
            (aliases / "ledger-link.csv").symlink_to(ledger)
            lock = data / ".ledger.jsonl.lock"
            lock.write_text("lock", encoding="utf-8")
            balances = data / "balances.csv"
            balances.write_text("balance", encoding="utf-8")
            exported = root / "exported.csv"
            exported.write_text("date,ledger_export_id\n2026-01-01,mentat-ledger-export-v1:synthetic\n", encoding="utf-8")
            directory = root / "directory.csv"
            directory.mkdir()
            sources = [aliases / "ledger-link.csv", aliases / "../data/ledger.jsonl", lock, balances, exported, directory]
            if hasattr(os, "mkfifo"):
                fifo = root / "pipe.csv"
                os.mkfifo(fifo)
                sources.append(fifo)
            for source in sources:
                with self.subTest(source=source):
                    code, _, errors = run_cli(
                        "ledger", "import-csv", "--root", root, "--input", source, "--profile", profile
                    )
                    self.assertEqual(code, 2)
                    self.assertIn("Refusing", errors)
            self.assertEqual(ledger.read_text(encoding="utf-8"), "secret ledger")

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_hardlinked_input_and_profile_cannot_alias_protected_runtime_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            ledger = data / "ledger.jsonl"
            ledger.write_text("protected", encoding="utf-8")
            source_alias = root / "source.csv"
            profile_alias = root / "profile.local.json"
            os.link(ledger, source_alias)
            os.link(ledger, profile_alias)

            source_code, _, source_errors = run_cli(
                "ledger", "import-csv", "--root", root,
                "--input", source_alias, "--profile", profile_alias,
            )
            profile_code, _, profile_errors = run_cli(
                "ledger", "import-csv", "--root", root,
                "--input", write_csv(root, ["12.01.2026;First;Note;1,00;EUR"]),
                "--profile", profile_alias,
            )

            self.assertEqual((source_code, profile_code), (2, 2))
            self.assertIn("reserved for Mentat runtime data", source_errors)
            self.assertIn("reserved for Mentat runtime data", profile_errors)
            self.assertEqual(ledger.read_text(encoding="utf-8"), "protected")

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "link"), "POSIX inode contract")
    def test_input_profile_aliases_are_refused_without_changing_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shared = write_profile_json(root, profile_value(), "shared.local.json")
            shared.chmod(0o644)
            original = shared.read_bytes()

            direct_code, _, direct_errors = run_cli(
                "ledger", "import-csv", "--root", root,
                "--input", shared, "--profile", shared,
            )
            hardlink = root / "shared-hardlink.local.json"
            os.link(shared, hardlink)
            hardlink_code, _, hardlink_errors = run_cli(
                "ledger", "import-csv", "--root", root,
                "--input", shared, "--profile", hardlink,
            )

            self.assertEqual((direct_code, hardlink_code), (2, 2))
            self.assertIn("different files", direct_errors)
            self.assertIn("different files", hardlink_errors)
            self.assertEqual(shared.read_bytes(), original)
            self.assertEqual(stat.S_IMODE(shared.stat().st_mode), 0o644)

    def test_local_profile_is_made_private_without_changing_source_permissions(self):
        if os.name != "posix":
            self.skipTest("POSIX permission contract")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            profile.chmod(0o644)
            source = write_csv(root, ["12.01.2026;First;Note;1,00;EUR"])
            source.chmod(0o644)

            code, _, errors = run_cli(
                "ledger", "import-csv", "--root", root, "--input", source, "--profile", profile
            )

            self.assertEqual(code, 0, errors)
            self.assertEqual(stat.S_IMODE(profile.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(source.stat().st_mode), 0o644)

    def test_source_is_immutable_after_failed_and_successful_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = write_profile_json(root, profile_value())
            source = write_csv(root, ["BAD_DATE;First;Note;1,00;EUR"])
            source.chmod(0o640)
            failed_bytes, failed_mode = source.read_bytes(), stat.S_IMODE(source.stat().st_mode)
            self.assertEqual(run_cli("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)[0], 2)
            self.assertEqual((source.read_bytes(), stat.S_IMODE(source.stat().st_mode)), (failed_bytes, failed_mode))
            source = write_csv(root, ["12.01.2026;First;Note;1,00;EUR"])
            source.chmod(0o640)
            success_bytes, success_mode = source.read_bytes(), stat.S_IMODE(source.stat().st_mode)
            self.assertEqual(run_cli("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)[0], 0)
            self.assertEqual((source.read_bytes(), stat.S_IMODE(source.stat().st_mode)), (success_bytes, success_mode))

    def test_outside_runtime_input_does_not_relax_ledger_write_boundary(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            profile = write_profile_json(outside, profile_value())
            source = write_csv(outside, ["12.01.2026;First;Note;1,00;EUR"])
            ledger = Path(outside) / "ledger.jsonl"

            code, _, errors = run_cli(
                "ledger", "import-csv", "--root", root, "--input", source, "--profile", profile, "--ledger", ledger
            )

            self.assertEqual(code, 2)
            self.assertIn("outside the runtime data directory", errors)
            self.assertFalse(ledger.exists())

    def test_period_override_supports_empty_data_while_missing_period_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            value = profile_value()
            value.pop("period")
            profile = write_profile_json(root, value)
            source = write_csv(root, [])
            arguments = ("ledger", "import-csv", "--root", root, "--input", source, "--profile", profile)
            code, _, _ = run_cli(*arguments)
            self.assertEqual(code, 2)
            self.assertFalse((root / "data" / "ledger.jsonl").exists())

            code, output, errors = run_cli(*arguments, "--period", "2026-01-01", "2026-01-31", "--preview")

            self.assertEqual(code, 0, errors)
            self.assertEqual(json.loads(output)["rows"], [])

    def test_renamed_profile_keeps_declared_source_ref_and_same_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_profile = write_profile_json(root, profile_value(), "first.local.json")
            source = write_csv(root, ["12.01.2026;First;Note;1,00;EUR"])
            base = ("ledger", "import-csv", "--root", root, "--input", source)
            self.assertEqual(run_cli(*base, "--profile", first_profile)[0], 0)
            renamed_profile = root / "renamed.local.json"
            first_profile.rename(renamed_profile)

            code, output, errors = run_cli(*base, "--profile", renamed_profile)

            self.assertEqual(code, 0, errors)
            self.assertIn("1 duplicate", output)
            records = JsonlLedger(root / "data" / "ledger.jsonl").records("transaction")
            self.assertEqual({record.provenance.source_ref for record in records}, {"synthetic-broker-primary"})


if __name__ == "__main__":
    unittest.main()
