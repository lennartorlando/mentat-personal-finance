"""Normalized local-ledger records and content-derived identifiers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Union


@dataclass(frozen=True)
class Diagnostic:
    input_name: str
    field: str
    message: str
    line: int | None = None
    record_id: str | None = None
    previous_value: str | None = None
    new_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "input": self.input_name,
            "field": self.field,
            "message": self.message,
        }
        if self.line is not None:
            value["line"] = self.line
        if self.record_id is not None:
            value["record_id"] = self.record_id
        if self.previous_value is not None:
            value["previous_value"] = self.previous_value
        if self.new_value is not None:
            value["new_value"] = self.new_value
        return value


class LedgerValidationError(ValueError):
    def __init__(self, diagnostics: Iterable[Diagnostic]):
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(f"{item.field}: {item.message}" for item in self.diagnostics))


@dataclass(frozen=True)
class Provenance:
    source: str
    imported_at: dt.datetime
    source_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "imported_at": _utc_isoformat(self.imported_at),
            "source_ref": self.source_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Provenance":
        value = _mapping(value, "provenance")
        return cls(
            source=_string(value, "source", "provenance"),
            imported_at=_parse_datetime(
                _string(value, "imported_at", "provenance"),
                "provenance.imported_at",
            ),
            source_ref=_string(value, "source_ref", "provenance"),
        )


@dataclass(frozen=True)
class AccountRecord:
    id: str
    source_account_id: str
    name: str | None
    currency: str | None
    provenance: Provenance
    record_type: str = "account"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "record_type": self.record_type,
            "source_account_id": self.source_account_id,
            "name": self.name,
            "currency": self.currency,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class BalanceRecord:
    id: str
    account_id: str
    balance_date: str
    snapshot_date: str
    amount: str
    account_identifiers: Mapping[str, str]
    provenance: Provenance
    record_type: str = "balance"

    def __post_init__(self) -> None:
        # Copy before wrapping so callers cannot retain an alias that mutates a
        # frozen record's identity after its IDs have been derived.
        object.__setattr__(
            self,
            "account_identifiers",
            MappingProxyType(dict(self.account_identifiers)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "record_type": self.record_type,
            "account_id": self.account_id,
            "balance_date": self.balance_date,
            "snapshot_date": self.snapshot_date,
            "amount": self.amount,
            "account_identifiers": dict(self.account_identifiers),
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class TransactionRecord:
    id: str
    account_id: str
    booking_date: str
    amount: str
    purpose: str
    normalized_purpose: str
    ordinal: int
    provenance: Provenance
    record_type: str = "transaction"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "record_type": self.record_type,
            "account_id": self.account_id,
            "booking_date": self.booking_date,
            "amount": self.amount,
            "purpose": self.purpose,
            "normalized_purpose": self.normalized_purpose,
            "ordinal": self.ordinal,
            "provenance": self.provenance.to_dict(),
        }


@dataclass(frozen=True)
class HoldingRecord:
    id: str
    account_id: str
    as_of: str
    asset: str
    quantity: str
    provenance: Provenance
    record_type: str = "holding"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "record_type": self.record_type,
            "account_id": self.account_id,
            "as_of": self.as_of,
            "asset": self.asset,
            "quantity": self.quantity,
            "provenance": self.provenance.to_dict(),
        }


LedgerRecord = Union[AccountRecord, BalanceRecord, TransactionRecord, HoldingRecord]


def stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}_{digest}"


def normalize_purpose(value: str) -> str:
    return " ".join(value.split()).casefold()


def account_record(
    source_account_id: str,
    provenance: Provenance,
    *,
    name: str | None = None,
    currency: str | None = None,
) -> AccountRecord:
    _require(source_account_id, "source_account_id", provenance.source_ref)
    return AccountRecord(
        id=_account_id(provenance.source, source_account_id),
        source_account_id=source_account_id,
        name=name,
        currency=currency,
        provenance=provenance,
    )


def balance_record_from_row(
    row: Mapping[str, str],
    *,
    input_name: str = "aqbanking:listbal",
    line: int | None = None,
) -> BalanceRecord:
    diagnostics: list[Diagnostic] = []
    for field in ("date", "source", "balance_date", "balance", "exported_at"):
        if not row.get(field, "").strip():
            diagnostics.append(Diagnostic(input_name, field, "is required", line))

    identifiers = {
        "iban": row.get("iban", "").strip(),
        "bank_code": row.get("bank_code", "").strip(),
        "account_number": row.get("account_number", "").strip(),
    }
    if not identifiers["iban"]:
        for field in ("bank_code", "account_number"):
            if not identifiers[field]:
                diagnostics.append(
                    Diagnostic(input_name, field, "is required when IBAN is not provided", line)
                )

    balance_date = _validated_date(row.get("balance_date", ""), "balance_date", input_name, diagnostics, line)
    snapshot_date = _validated_date(row.get("date", ""), "date", input_name, diagnostics, line)
    imported_at = _validated_datetime(
        row.get("exported_at", ""), "exported_at", input_name, diagnostics, line
    )
    if diagnostics:
        raise LedgerValidationError(diagnostics)

    provenance = Provenance(row["source"], imported_at, input_name)
    account_id = _account_id(provenance.source, _balance_source_account_id(identifiers))
    return BalanceRecord(
        id=stable_id("bal", provenance.source, account_id, balance_date),
        account_id=account_id,
        balance_date=balance_date,
        snapshot_date=snapshot_date,
        amount=row["balance"],
        account_identifiers=identifiers,
        provenance=provenance,
    )


def transaction_records(
    rows: Iterable[Mapping[str, str]],
    provenance: Provenance,
    *,
    complete_source_slice: bool,
    input_name: str | None = None,
) -> list[TransactionRecord]:
    """Build records from one complete source slice, preserving repeated payments.

    The ordinal tiebreaker is only stable when every transaction in the source
    slice is supplied together. Incremental batches cannot distinguish a replay
    from a new, content-identical payment and are therefore rejected.
    """
    source_name = input_name or provenance.source_ref
    if not complete_source_slice:
        raise ValueError("transaction_records requires a complete source slice")

    ordinals: dict[tuple[str, str, str, str], int] = {}
    records: list[TransactionRecord] = []
    for line, row in enumerate(rows, start=1):
        diagnostics: list[Diagnostic] = []
        for field in ("account_id", "booking_date", "amount", "purpose"):
            if not row.get(field, "").strip():
                diagnostics.append(Diagnostic(source_name, field, "is required", line))
        booking_date = _validated_date(row.get("booking_date", ""), "booking_date", source_name, diagnostics, line)
        if diagnostics:
            raise LedgerValidationError(diagnostics)

        account_id = row["account_id"]
        purpose = row["purpose"]
        normalized = normalize_purpose(purpose)
        key = (account_id, booking_date, row["amount"], normalized)
        ordinal = ordinals.get(key, 0)
        ordinals[key] = ordinal + 1
        records.append(
            TransactionRecord(
                id=stable_id(
                    "txn",
                    provenance.source,
                    account_id,
                    booking_date,
                    row["amount"],
                    normalized,
                    ordinal,
                ),
                account_id=account_id,
                booking_date=booking_date,
                amount=row["amount"],
                purpose=purpose,
                normalized_purpose=normalized,
                ordinal=ordinal,
                provenance=provenance,
            )
        )
    return records


def holding_record(
    account_id: str,
    as_of: str,
    asset: str,
    quantity: str,
    provenance: Provenance,
) -> HoldingRecord:
    source_name = provenance.source_ref
    diagnostics: list[Diagnostic] = []
    for field, value in (
        ("account_id", account_id),
        ("as_of", as_of),
        ("asset", asset),
        ("quantity", quantity),
    ):
        if not value.strip():
            diagnostics.append(Diagnostic(source_name, field, "is required"))
    normalized_date = _validated_date(as_of, "as_of", source_name, diagnostics)
    if diagnostics:
        raise LedgerValidationError(diagnostics)
    return HoldingRecord(
        id=stable_id("hold", provenance.source, account_id, normalized_date, asset),
        account_id=account_id,
        as_of=normalized_date,
        asset=asset,
        quantity=quantity,
        provenance=provenance,
    )


def record_from_dict(value: object) -> LedgerRecord:
    value = _mapping(value, "ledger record")
    record_type = _string(value, "record_type", "ledger record")
    provenance = Provenance.from_dict(_required(value, "provenance", "ledger record"))
    if record_type == "account":
        source_account_id = _string(value, "source_account_id", "account")
        record = AccountRecord(
            id=_string(value, "id", "account"),
            source_account_id=source_account_id,
            name=_optional_string(value.get("name"), "account.name"),
            currency=_optional_string(value.get("currency"), "account.currency"),
            provenance=provenance,
        )
        _verify_id(
            record.id,
            _account_id(provenance.source, source_account_id),
            "account",
        )
        return record
    if record_type == "balance":
        identifiers_value = _mapping(
            _required(value, "account_identifiers", "balance"),
            "balance.account_identifiers",
        )
        for key, item in identifiers_value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError("balance.account_identifiers keys and values must be strings")
        identifiers = dict(identifiers_value)
        for field in ("iban", "bank_code", "account_number"):
            _string(identifiers, field, "balance.account_identifiers", allow_empty=True)
        if not identifiers["iban"]:
            if not identifiers["bank_code"] or not identifiers["account_number"]:
                raise ValueError(
                    "balance.account_identifiers requires bank_code and account_number when IBAN is empty"
                )
        expected_account_id = _account_id(
            provenance.source, _balance_source_account_id(identifiers)
        )
        legacy_account_id = _legacy_balance_account_id(provenance.source, identifiers)
        account_id = _string(value, "account_id", "balance")
        if account_id not in (expected_account_id, legacy_account_id):
            raise ValueError("balance.account_id does not match its account identifiers")
        balance_date = _canonical_date(value, "balance_date", "balance")
        record = BalanceRecord(
            id=_string(value, "id", "balance"),
            account_id=account_id,
            balance_date=balance_date,
            snapshot_date=_canonical_date(value, "snapshot_date", "balance"),
            amount=_string(value, "amount", "balance"),
            account_identifiers=identifiers,
            provenance=provenance,
        )
        _verify_id(
            record.id,
            stable_id("bal", provenance.source, account_id, balance_date),
            "balance",
        )
        return record
    if record_type == "transaction":
        account_id = _string(value, "account_id", "transaction")
        booking_date = _canonical_date(value, "booking_date", "transaction")
        amount = _string(value, "amount", "transaction")
        purpose = _string(value, "purpose", "transaction")
        normalized_purpose = _string(value, "normalized_purpose", "transaction")
        if normalized_purpose != normalize_purpose(purpose):
            raise ValueError("transaction.normalized_purpose does not match purpose")
        ordinal = _integer(value, "ordinal", "transaction")
        if ordinal < 0:
            raise ValueError("transaction.ordinal must be nonnegative")
        record = TransactionRecord(
            id=_string(value, "id", "transaction"),
            account_id=account_id,
            booking_date=booking_date,
            amount=amount,
            purpose=purpose,
            normalized_purpose=normalized_purpose,
            ordinal=ordinal,
            provenance=provenance,
        )
        _verify_id(
            record.id,
            stable_id(
                "txn",
                provenance.source,
                account_id,
                booking_date,
                amount,
                normalized_purpose,
                ordinal,
            ),
            "transaction",
        )
        return record
    if record_type == "holding":
        account_id = _string(value, "account_id", "holding")
        as_of = _canonical_date(value, "as_of", "holding")
        asset = _string(value, "asset", "holding")
        record = HoldingRecord(
            id=_string(value, "id", "holding"),
            account_id=account_id,
            as_of=as_of,
            asset=asset,
            quantity=_string(value, "quantity", "holding"),
            provenance=provenance,
        )
        _verify_id(
            record.id,
            stable_id("hold", provenance.source, account_id, as_of, asset),
            "holding",
        )
        return record
    raise ValueError(f"Unknown ledger record type: {record_type!r}")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _required(value: Mapping[str, Any], field: str, parent: str) -> Any:
    if field not in value:
        raise ValueError(f"{parent}.{field} is required")
    return value[field]


def _string(
    value: Mapping[str, Any],
    field: str,
    parent: str,
    *,
    allow_empty: bool = False,
) -> str:
    item = _required(value, field, parent)
    if not isinstance(item, str):
        raise ValueError(f"{parent}.{field} must be a string")
    if not allow_empty and not item.strip():
        raise ValueError(f"{parent}.{field} is required")
    return item


def _integer(value: Mapping[str, Any], field: str, parent: str) -> int:
    item = _required(value, field, parent)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{parent}.{field} must be an integer")
    return item


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string or null")
    return value


def _canonical_date(value: Mapping[str, Any], field: str, parent: str) -> str:
    item = _string(value, field, parent)
    try:
        parsed = dt.date.fromisoformat(item)
    except ValueError as exc:
        raise ValueError(f"{parent}.{field} must be YYYY-MM-DD") from exc
    if parsed.isoformat() != item:
        raise ValueError(f"{parent}.{field} must be YYYY-MM-DD")
    return item


def _verify_id(actual: str, expected: str, record_type: str) -> None:
    if actual != expected:
        raise ValueError(f"{record_type}.id does not match its derived identity")


def _account_id(source: str, source_account_id: str) -> str:
    return stable_id("acct", source, "source_account_id", source_account_id)


def _balance_source_account_id(identifiers: Mapping[str, str]) -> str:
    return identifiers["iban"] or f"{identifiers['bank_code']}:{identifiers['account_number']}"


def _legacy_balance_account_id(source: str, identifiers: Mapping[str, str]) -> str:
    if identifiers["iban"]:
        return stable_id("acct", source, "iban", identifiers["iban"])
    return stable_id(
        "acct",
        source,
        "bank_code",
        identifiers["bank_code"],
        "account_number",
        identifiers["account_number"],
    )


def canonical_balance_record(record: BalanceRecord) -> BalanceRecord:
    """Return a balance using the current account identity without changing its content."""
    account_id = _account_id(
        record.provenance.source,
        _balance_source_account_id(record.account_identifiers),
    )
    if record.account_id == account_id:
        return record
    return BalanceRecord(
        id=stable_id("bal", record.provenance.source, account_id, record.balance_date),
        account_id=account_id,
        balance_date=record.balance_date,
        snapshot_date=record.snapshot_date,
        amount=record.amount,
        account_identifiers=record.account_identifiers,
        provenance=record.provenance,
    )


def _require(value: str, field: str, input_name: str) -> None:
    if not value.strip():
        raise LedgerValidationError([Diagnostic(input_name, field, "is required")])


def _validated_date(
    value: str,
    field: str,
    input_name: str,
    diagnostics: list[Diagnostic],
    line: int | None = None,
) -> str:
    if not value:
        return ""
    for pattern in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            pass
    diagnostics.append(Diagnostic(input_name, field, "must be YYYY-MM-DD or DD.MM.YYYY", line))
    return ""


def _validated_datetime(
    value: str,
    field: str,
    input_name: str,
    diagnostics: list[Diagnostic],
    line: int | None = None,
) -> dt.datetime:
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        return _parse_datetime(value, field)
    except ValueError:
        diagnostics.append(Diagnostic(input_name, field, "must be an ISO-8601 timestamp", line))
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)


def _parse_datetime(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Invalid {field}: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Invalid {field}: timezone is required")
    return parsed


def _utc_isoformat(value: dt.datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Provenance imported_at must include a timezone.")
    return value.astimezone(dt.timezone.utc).isoformat()
