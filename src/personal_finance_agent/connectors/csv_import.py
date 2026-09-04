from __future__ import annotations

# Declared-format CSV parsing with value-free diagnostics.

import csv
import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Set, Tuple

from ..ledger import Diagnostic
from .csv_profile import CsvProfile


CSV_INPUT_NAME = "csv-import"
ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
NUMBER = re.compile(r"^[+-]?[0-9]+(?:\.[0-9]+)?$")
DENOMINATION = re.compile(r"^[A-Z0-9]+$")


@dataclass(frozen=True)
class CsvParseResult:
    rows: Tuple[Dict[str, str], ...]
    diagnostics: Tuple[Diagnostic, ...]


class _DecodeAtLine(UnicodeError):
    def __init__(self, line: int):
        self.line = line
        super().__init__("declared encoding could not decode input")


def parse_csv(path: Path, profile: CsvProfile) -> CsvParseResult:
    diagnostics = []  # type: List[Diagnostic]
    rows = []  # type: List[Dict[str, str]]
    try:
        with Path(path).open("rb") as handle:
            reader = csv.reader(
                _decoded_lines(handle, profile.format.encoding),
                delimiter=profile.format.delimiter,
                quotechar=profile.format.quotechar,
                strict=True,
            )
            header, positions = _find_header(reader, profile, diagnostics)
            if diagnostics or header is None or positions is None:
                return CsvParseResult((), tuple(diagnostics))
            previous_line = reader.line_num
            while True:
                start_line = previous_line + 1
                try:
                    cells = next(reader)
                except StopIteration:
                    break
                except _DecodeAtLine as exc:
                    diagnostics.append(
                        Diagnostic(
                            CSV_INPUT_NAME,
                            "encoding",
                            "input is not valid for format.encoding",
                            exc.line,
                        )
                    )
                    break
                except csv.Error as exc:
                    if "NUL" in str(exc):
                        diagnostics.append(Diagnostic(CSV_INPUT_NAME, "input", "is not text", start_line))
                    else:
                        diagnostics.append(
                            Diagnostic(CSV_INPUT_NAME, "record", "is not valid CSV for the declared format", start_line)
                        )
                    break
                previous_line = reader.line_num
                if _contains_nul(cells):
                    diagnostics.append(Diagnostic(CSV_INPUT_NAME, "input", "is not text", start_line))
                    break
                if not cells or all(not cell.strip() for cell in cells):
                    continue
                if len(cells) != len(header):
                    diagnostics.append(
                        Diagnostic(
                            CSV_INPUT_NAME,
                            "record",
                            "line has {} column(s); header has {}".format(len(cells), len(header)),
                            start_line,
                        )
                    )
                    continue
                parsed = _parse_row(cells, positions, profile, start_line, diagnostics)
                if parsed is not None:
                    rows.append(parsed)
    except _DecodeAtLine as exc:
        diagnostics.append(
            Diagnostic(CSV_INPUT_NAME, "encoding", "input is not valid for format.encoding", exc.line)
        )
    except (OSError, csv.Error):
        diagnostics.append(Diagnostic(CSV_INPUT_NAME, "input", "could not be read as declared CSV"))

    if diagnostics:
        return CsvParseResult((), tuple(diagnostics))
    return CsvParseResult(tuple(rows), ())


def _decoded_lines(handle: object, encoding: str) -> Iterator[str]:
    for line_number, raw_line in enumerate(handle, start=1):
        try:
            yield raw_line.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            raise _DecodeAtLine(line_number)


def _find_header(
    reader: csv.reader,
    profile: CsvProfile,
    diagnostics: List[Diagnostic],
) -> Tuple[Optional[List[str]], Optional[Dict[str, int]]]:
    candidate_descriptions = []  # type: List[str]
    declared = profile.declared_columns()
    required_names = set(name for _, name in declared)
    try:
        for cells in reader:
            if _contains_nul(cells):
                diagnostics.append(Diagnostic(CSV_INPUT_NAME, "input", "is not text", reader.line_num))
                return None, None
            if not cells or all(not cell.strip() for cell in cells):
                continue
            cells = list(cells)
            cells[0] = cells[0].lstrip("\ufeff")
            names = set(cells)
            matched = required_names.intersection(names)
            if required_names.issubset(names):
                return cells, {name: cells.index(name) for name in required_names}
            if len(candidate_descriptions) < 20:
                candidate_descriptions.append(_candidate_description(cells))
            if len(matched) >= max(1, len(required_names) - 1):
                for profile_key, name in declared:
                    if name not in names:
                        diagnostics.append(
                            Diagnostic(
                                CSV_INPUT_NAME,
                                "header",
                                "{} names missing column {!r}".format(profile_key, _safe_header_name(name)),
                                reader.line_num,
                            )
                        )
                unexpected = [name for name in cells if name not in required_names]
                for name in unexpected[:5]:
                    diagnostics.append(
                        Diagnostic(
                            CSV_INPUT_NAME,
                            "header",
                            "confirmed header contains undeclared column {!r}".format(
                                _safe_header_name(name)
                            ),
                            reader.line_num,
                        )
                    )
                return None, None
    except _DecodeAtLine:
        raise
    except csv.Error as exc:
        if "NUL" in str(exc):
            diagnostics.append(Diagnostic(CSV_INPUT_NAME, "input", "is not text", reader.line_num + 1))
        else:
            diagnostics.append(
                Diagnostic(CSV_INPUT_NAME, "header", "could not be read using the declared CSV format", reader.line_num + 1)
            )
        return None, None
    summaries = "; ".join(candidate_descriptions[:20]) or "no non-blank candidate rows"
    diagnostics.append(
        Diagnostic(CSV_INPUT_NAME, "header", "no declared header matched; candidates: " + summaries)
    )
    return None, None


def _parse_row(
    cells: List[str],
    positions: Dict[str, int],
    profile: CsvProfile,
    line: int,
    diagnostics: List[Diagnostic],
) -> Optional[Dict[str, str]]:
    initial_count = len(diagnostics)
    date_index = positions[profile.columns.booking_date]
    try:
        booking_date = dt.datetime.strptime(
            cells[date_index].strip(), profile.format.date_format
        ).date().isoformat()
    except ValueError:
        diagnostics.append(
            Diagnostic(
                CSV_INPUT_NAME,
                "booking_date",
                "columns.booking_date at column {} does not match format.date_format {!r}".format(
                    date_index + 1, profile.format.date_format
                ),
                line,
            )
        )
        booking_date = ""

    purpose_parts = [cells[positions[name]].strip() for name in profile.columns.purpose]
    purpose = " | ".join(part for part in purpose_parts if part)
    if not purpose:
        first_index = positions[profile.columns.purpose[0]]
        diagnostics.append(
            Diagnostic(
                CSV_INPUT_NAME,
                "purpose",
                "columns.purpose is empty (starting at column {})".format(first_index + 1),
                line,
            )
        )

    denomination_index = positions[profile.columns.denomination]
    denomination = cells[denomination_index].strip().upper()
    if not DENOMINATION.fullmatch(denomination):
        diagnostics.append(
            Diagnostic(
                CSV_INPUT_NAME,
                "denomination",
                "columns.denomination at column {} must be alphanumeric".format(denomination_index + 1),
                line,
            )
        )

    amount = _row_amount(cells, positions, profile, line, diagnostics)
    if len(diagnostics) != initial_count:
        return None
    return {"booking_date": booking_date, "amount": amount + " " + denomination, "purpose": purpose}


def _row_amount(
    cells: List[str],
    positions: Dict[str, int],
    profile: CsvProfile,
    line: int,
    diagnostics: List[Diagnostic],
) -> str:
    amount = profile.amount
    negative = False
    profile_key = "amount.column"
    column_name = amount.column or ""
    raw = ""
    if amount.strategy in ("signed", "inverted"):
        raw = cells[positions[column_name]].strip()
        negative = raw.startswith("-")
        if amount.strategy == "inverted":
            negative = not negative
    elif amount.strategy == "debit_credit_columns":
        debit = cells[positions[amount.debit_column or ""]].strip()
        credit = cells[positions[amount.credit_column or ""]].strip()
        if bool(debit) == bool(credit):
            diagnostics.append(
                Diagnostic(
                    CSV_INPUT_NAME,
                    "amount",
                    "amount.debit_column and amount.credit_column must have exactly one populated column",
                    line,
                )
            )
            return ""
        negative = bool(debit)
        raw = debit or credit
        profile_key = "amount.debit_column" if debit else "amount.credit_column"
        column_name = amount.debit_column if debit else amount.credit_column
    else:
        raw = cells[positions[column_name]].strip()
        indicator = cells[positions[amount.indicator_column or ""]].strip()
        if indicator in amount.debit_values:
            negative = True
        elif indicator in amount.credit_values:
            negative = False
        else:
            index = positions[amount.indicator_column or ""]
            diagnostics.append(
                Diagnostic(
                    CSV_INPUT_NAME,
                    "amount_indicator",
                    "amount.indicator_column at column {} is not in a declared indicator set".format(index + 1),
                    line,
                )
            )
            return ""
    index = positions[column_name or ""]
    normalized = _normalize_number(raw, profile, negative)
    if normalized is None:
        diagnostics.append(
            Diagnostic(
                CSV_INPUT_NAME,
                "amount",
                "{} at column {} is not a number in the declared format".format(profile_key, index + 1),
                line,
            )
        )
        return ""
    return normalized


def _normalize_number(raw: str, profile: CsvProfile, negative: bool) -> Optional[str]:
    value = raw.strip()
    if profile.format.thousands_separator:
        value = value.replace(profile.format.thousands_separator, "")
    if profile.format.decimal_mark != ".":
        value = value.replace(profile.format.decimal_mark, ".")
    if not NUMBER.fullmatch(value):
        return None
    unsigned = value.lstrip("+-")
    if "." not in unsigned:
        unsigned += ".0"
    integer, decimals = unsigned.split(".", 1)
    integer = integer.lstrip("0") or "0"
    if integer == "0" and not any(char != "0" for char in decimals):
        negative = False
    return ("-" if negative else "") + integer + "." + decimals


def _contains_nul(cells: Iterable[str]) -> bool:
    return any("\x00" in cell for cell in cells)


def _candidate_description(cells: List[str]) -> str:
    classes = set()  # type: Set[str]
    for cell in cells:
        for char in cell:
            if char.isalpha():
                classes.add("letters")
            elif char.isdigit():
                classes.add("digits")
            elif char.isspace():
                classes.add("whitespace")
            elif ord(char) < 32 or ord(char) == 127:
                classes.add("control")
            else:
                classes.add("punctuation")
    return "{} column(s) containing {}".format(len(cells), ", ".join(sorted(classes)) or "empty cells")


def _safe_header_name(value: str) -> str:
    cleaned = ANSI_ESCAPE.sub("", value)
    cleaned = "".join(char for char in cleaned if ord(char) >= 32 and ord(char) != 127)
    return cleaned[:80]
