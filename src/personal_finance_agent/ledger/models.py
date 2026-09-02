"""Normalized local-ledger records and content-derived identifiers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Union


@dataclass(frozen=True)
class Diagnostic:
    input_name: str
    field: str
    message: str
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "input": self.input_name,
            "field": self.field,
            "message": self.message,
        }
        if self.line is not None:
            value["line"] = self.line
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
        return cls(
            source=str(value["source"]),
            imported_at=_parse_datetime(str(value["imported_at"]), "provenance.imported_at"),
            source_ref=str(value["source_ref"]),
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
    account_identifiers: dict[str, str]
    provenance: Provenance
    record_type: str = "balance"

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
        id=stable_id("acct", provenance.source, "source_account_id", source_account_id),
        source_account_id=source_account_id,
        name=name,
        currency=currency,
        provenance=provenance,
    )


def balance_record_from_row(row: Mapping[str, str], *, input_name: str = "aqbanking:listbal") -> BalanceRecord:
    diagnostics: list[Diagnostic] = []
    for field in ("date", "source", "balance_date", "balance", "exported_at"):
        if not row.get(field, "").strip():
            diagnostics.append(Diagnostic(input_name, field, "is required"))

    identifiers = {
        "iban": row.get("iban", "").strip(),
        "bank_code": row.get("bank_code", "").strip(),
        "account_number": row.get("account_number", "").strip(),
    }
    if not identifiers["iban"]:
        for field in ("bank_code", "account_number"):
            if not identifiers[field]:
                diagnostics.append(
                    Diagnostic(input_name, field, "is required when IBAN is not provided")
                )

    balance_date = _validated_date(row.get("balance_date", ""), "balance_date", input_name, diagnostics)
    snapshot_date = _validated_date(row.get("date", ""), "date", input_name, diagnostics)
    imported_at = _validated_datetime(row.get("exported_at", ""), "exported_at", input_name, diagnostics)
    if diagnostics:
        raise LedgerValidationError(diagnostics)

    provenance = Provenance(row["source"], imported_at, input_name)
    if identifiers["iban"]:
        account_id = stable_id("acct", provenance.source, "iban", identifiers["iban"])
    else:
        account_id = stable_id(
            "acct",
            provenance.source,
            "bank_code",
            identifiers["bank_code"],
            "account_number",
            identifiers["account_number"],
        )
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
    for field, value in (("account_id", account_id), ("asset", asset), ("quantity", quantity)):
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


def record_from_dict(value: Mapping[str, Any]) -> LedgerRecord:
    record_type = value.get("record_type")
    provenance = Provenance.from_dict(value["provenance"])
    if record_type == "account":
        return AccountRecord(
            id=str(value["id"]),
            source_account_id=str(value["source_account_id"]),
            name=_optional_string(value.get("name")),
            currency=_optional_string(value.get("currency")),
            provenance=provenance,
        )
    if record_type == "balance":
        return BalanceRecord(
            id=str(value["id"]),
            account_id=str(value["account_id"]),
            balance_date=str(value["balance_date"]),
            snapshot_date=str(value["snapshot_date"]),
            amount=str(value["amount"]),
            account_identifiers={str(key): str(item) for key, item in value["account_identifiers"].items()},
            provenance=provenance,
        )
    if record_type == "transaction":
        return TransactionRecord(
            id=str(value["id"]),
            account_id=str(value["account_id"]),
            booking_date=str(value["booking_date"]),
            amount=str(value["amount"]),
            purpose=str(value["purpose"]),
            normalized_purpose=str(value["normalized_purpose"]),
            ordinal=int(value["ordinal"]),
            provenance=provenance,
        )
    if record_type == "holding":
        return HoldingRecord(
            id=str(value["id"]),
            account_id=str(value["account_id"]),
            as_of=str(value["as_of"]),
            asset=str(value["asset"]),
            quantity=str(value["quantity"]),
            provenance=provenance,
        )
    raise ValueError(f"Unknown ledger record type: {record_type!r}")


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


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
) -> dt.datetime:
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    try:
        return _parse_datetime(value, field)
    except ValueError:
        diagnostics.append(Diagnostic(input_name, field, "must be an ISO-8601 timestamp"))
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
