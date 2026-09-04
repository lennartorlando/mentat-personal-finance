from __future__ import annotations

# Validated JSON profiles for declared CSV import formats.

import codecs
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Mapping, Optional, Tuple

from ..ledger import Diagnostic


PROFILE_INPUT_NAME = "csv-profile"
AMOUNT_STRATEGIES = frozenset(("signed", "inverted", "debit_credit_columns", "indicator"))
IDENTIFIER_KINDS = frozenset(("iban", "opaque"))
SAFE_SOURCE_REF = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,63}$")


class CsvProfileError(ValueError):
    """A profile could not be loaded without guessing its intended shape."""

    def __init__(self, diagnostics: Tuple[Diagnostic, ...]):
        self.diagnostics = diagnostics
        super().__init__("; ".join(item.field + ": " + item.message for item in diagnostics))


@dataclass(frozen=True)
class AccountProfile:
    identifier: str
    identifier_kind: str
    name: Optional[str]
    currency: Optional[str]


@dataclass(frozen=True)
class ImportPeriod:
    start: str
    end: str


@dataclass(frozen=True)
class CsvFormatProfile:
    encoding: str
    delimiter: str
    quotechar: str
    decimal_mark: str
    thousands_separator: str
    date_format: str


@dataclass(frozen=True)
class ColumnProfile:
    booking_date: str
    purpose: Tuple[str, ...]
    denomination: str


@dataclass(frozen=True)
class AmountProfile:
    strategy: str
    column: Optional[str] = None
    debit_column: Optional[str] = None
    credit_column: Optional[str] = None
    indicator_column: Optional[str] = None
    debit_values: Tuple[str, ...] = ()
    credit_values: Tuple[str, ...] = ()

    def column_keys(self) -> Tuple[Tuple[str, str], ...]:
        if self.strategy in ("signed", "inverted"):
            return (("amount.column", self.column or ""),)
        if self.strategy == "debit_credit_columns":
            return (
                ("amount.debit_column", self.debit_column or ""),
                ("amount.credit_column", self.credit_column or ""),
            )
        return (
            ("amount.column", self.column or ""),
            ("amount.indicator_column", self.indicator_column or ""),
        )


@dataclass(frozen=True)
class CsvProfile:
    source: str
    source_ref: str
    account: AccountProfile
    period: Optional[ImportPeriod]
    format: CsvFormatProfile
    columns: ColumnProfile
    amount: AmountProfile

    def declared_columns(self) -> Tuple[Tuple[str, str], ...]:
        values = [
            ("columns.booking_date", self.columns.booking_date),
            ("columns.denomination", self.columns.denomination),
        ]
        values.extend(
            ("columns.purpose[{}]".format(index), column)
            for index, column in enumerate(self.columns.purpose)
        )
        values.extend(self.amount.column_keys())
        return tuple(values)


def normalize_source_account_id(identifier: str, identifier_kind: str) -> str:
    if identifier_kind == "iban":
        return "".join(identifier.split()).upper()
    if identifier_kind == "opaque":
        return identifier.strip()
    raise ValueError("identifier kind must be iban or opaque")


def load_csv_profile(path: Path) -> CsvProfile:
    diagnostics = []  # type: List[Diagnostic]
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        line = getattr(exc, "lineno", None)
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, "profile", "is not valid UTF-8 JSON", line))
        raise CsvProfileError(tuple(diagnostics))

    if not isinstance(value, dict):
        raise CsvProfileError((Diagnostic(PROFILE_INPUT_NAME, "profile", "must be a JSON object"),))

    source = _required_string(value, "source", diagnostics)
    source_ref = _required_string(value, "source_ref", diagnostics)
    if source_ref and not SAFE_SOURCE_REF.fullmatch(source_ref):
        diagnostics.append(
            Diagnostic(
                PROFILE_INPUT_NAME,
                "source_ref",
                "must be a non-sensitive label using only letters, digits, dot, colon, underscore, or hyphen",
            )
        )

    account_value = _required_object(value, "account", diagnostics)
    identifier = _required_string(account_value, "identifier", diagnostics, "account.identifier")
    identifier_kind = _required_string(
        account_value, "identifier_kind", diagnostics, "account.identifier_kind"
    )
    if identifier_kind and identifier_kind not in IDENTIFIER_KINDS:
        diagnostics.append(
            Diagnostic(PROFILE_INPUT_NAME, "account.identifier_kind", "must be iban or opaque")
        )
    name = _optional_string(account_value, "name", diagnostics, "account.name")
    currency = _optional_string(account_value, "currency", diagnostics, "account.currency")

    period = _period(value.get("period"), diagnostics)

    format_value = _required_object(value, "format", diagnostics)
    encoding = _required_string(format_value, "encoding", diagnostics, "format.encoding")
    if encoding:
        try:
            codecs.lookup(encoding)
        except LookupError:
            diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, "format.encoding", "is not a known encoding"))
    delimiter = _required_string(format_value, "delimiter", diagnostics, "format.delimiter")
    quotechar = _required_string(format_value, "quotechar", diagnostics, "format.quotechar")
    decimal_mark = _required_string(format_value, "decimal_mark", diagnostics, "format.decimal_mark")
    thousands_separator = _declared_string(
        format_value, "thousands_separator", diagnostics, "format.thousands_separator"
    )
    date_format = _required_string(format_value, "date_format", diagnostics, "format.date_format")
    for field, item in (
        ("format.delimiter", delimiter),
        ("format.quotechar", quotechar),
        ("format.decimal_mark", decimal_mark),
    ):
        if item and len(item) != 1:
            diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, field, "must contain exactly one character"))
    if len(thousands_separator) > 1:
        diagnostics.append(
            Diagnostic(PROFILE_INPUT_NAME, "format.thousands_separator", "must contain at most one character")
        )
    separators = [item for item in (delimiter, quotechar, decimal_mark, thousands_separator) if item]
    if len(separators) != len(set(separators)):
        diagnostics.append(
            Diagnostic(PROFILE_INPUT_NAME, "format", "delimiter, quote character, and number separators must differ")
        )
    if date_format:
        try:
            dt.datetime.strptime("2001-02-03", date_format)
        except ValueError as exc:
            if "bad directive" in str(exc) or "stray %" in str(exc):
                diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, "format.date_format", "is not a valid date format"))

    columns_value = _required_object(value, "columns", diagnostics)
    booking_date = _required_string(
        columns_value, "booking_date", diagnostics, "columns.booking_date"
    )
    denomination = _required_string(
        columns_value, "denomination", diagnostics, "columns.denomination"
    )
    purpose = _string_list(columns_value, "purpose", diagnostics, "columns.purpose")

    amount_value = _required_object(value, "amount", diagnostics)
    strategy = _required_string(amount_value, "strategy", diagnostics, "amount.strategy")
    if strategy and strategy not in AMOUNT_STRATEGIES:
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, "amount.strategy", "is not a supported strategy"))
    amount = _amount_profile(amount_value, strategy, diagnostics)

    if diagnostics:
        raise CsvProfileError(tuple(diagnostics))
    normalized_identifier = normalize_source_account_id(identifier, identifier_kind)
    if not normalized_identifier:
        raise CsvProfileError((Diagnostic(PROFILE_INPUT_NAME, "account.identifier", "must not be empty"),))
    return CsvProfile(
        source=source,
        source_ref=source_ref,
        account=AccountProfile(normalized_identifier, identifier_kind, name, currency),
        period=period,
        format=CsvFormatProfile(
            encoding, delimiter, quotechar, decimal_mark, thousands_separator, date_format
        ),
        columns=ColumnProfile(booking_date, purpose, denomination),
        amount=amount,
    )


def _amount_profile(value: Mapping[str, Any], strategy: str, diagnostics: list) -> AmountProfile:
    if strategy in ("signed", "inverted"):
        column = _required_string(value, "column", diagnostics, "amount.column")
        return AmountProfile(strategy, column=column)
    if strategy == "debit_credit_columns":
        debit = _required_string(value, "debit_column", diagnostics, "amount.debit_column")
        credit = _required_string(value, "credit_column", diagnostics, "amount.credit_column")
        return AmountProfile(strategy, debit_column=debit, credit_column=credit)
    if strategy == "indicator":
        column = _required_string(value, "column", diagnostics, "amount.column")
        indicator = _required_string(
            value, "indicator_column", diagnostics, "amount.indicator_column"
        )
        debits = _string_list(value, "debit_values", diagnostics, "amount.debit_values")
        credits = _string_list(value, "credit_values", diagnostics, "amount.credit_values")
        if set(debits).intersection(credits):
            diagnostics.append(
                Diagnostic(PROFILE_INPUT_NAME, "amount", "debit and credit indicator sets must not overlap")
            )
        return AmountProfile(
            strategy,
            column=column,
            indicator_column=indicator,
            debit_values=debits,
            credit_values=credits,
        )
    return AmountProfile(strategy)


def _period(value: object, diagnostics: list) -> Optional[ImportPeriod]:
    if value is None:
        return None
    if not isinstance(value, dict):
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, "period", "must be a JSON object"))
        return None
    start = _required_string(value, "start", diagnostics, "period.start")
    end = _required_string(value, "end", diagnostics, "period.end")
    normalized = []
    for field, item in (("period.start", start), ("period.end", end)):
        try:
            normalized.append(dt.datetime.strptime(item, "%Y-%m-%d").date().isoformat())
        except ValueError:
            diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, field, "must use YYYY-MM-DD"))
            normalized.append("")
    if all(normalized) and normalized[0] > normalized[1]:
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, "period", "start must not be after end"))
    if not all(normalized):
        return None
    return ImportPeriod(normalized[0], normalized[1])


def _required_object(value: Mapping[str, Any], key: str, diagnostics: list) -> Mapping[str, Any]:
    item = value.get(key)
    if not isinstance(item, dict):
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, key, "must be a JSON object"))
        return {}
    return item


def _required_string(
    value: Mapping[str, Any], key: str, diagnostics: list, field: Optional[str] = None
) -> str:
    item = value.get(key)
    field = field or key
    if not isinstance(item, str) or not item.strip():
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, field, "must be a non-empty string"))
        return ""
    return item.strip()


def _declared_string(value: Mapping[str, Any], key: str, diagnostics: list, field: str) -> str:
    if key not in value or not isinstance(value[key], str):
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, field, "must be declared as a string"))
        return ""
    return value[key]


def _optional_string(
    value: Mapping[str, Any], key: str, diagnostics: list, field: str
) -> Optional[str]:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str):
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, field, "must be a string or null"))
        return None
    return item.strip() or None


def _string_list(value: Mapping[str, Any], key: str, diagnostics: list, field: str) -> Tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, list) or not item:
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, field, "must be a non-empty array of strings"))
        return ()
    if any(not isinstance(entry, str) or not entry.strip() for entry in item):
        diagnostics.append(Diagnostic(PROFILE_INPUT_NAME, field, "must contain only non-empty strings"))
        return ()
    return tuple(entry.strip() for entry in item)
