"""Ledger export adapters. Spreadsheet neutralization happens only here."""

from __future__ import annotations

import csv
import hashlib
import os
import tempfile
from pathlib import Path
from typing import Iterable

from ..private_files import (
    ensure_private_dir,
    ensure_private_file,
    fsync_parent_directory,
    private_file_lock,
)
from .models import BalanceRecord
from .store import ledger_lineage_id


SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
LEDGER_EXPORT_FIELD = "ledger_export_id"
LEDGER_EXPORT_VERSION = "mentat-ledger-export-v1"


def spreadsheet_safe_cell(value: str) -> str:
    if value.startswith(SPREADSHEET_FORMULA_PREFIXES):
        return "'" + value
    return value


def spreadsheet_safe_row(row: dict[str, str]) -> dict[str, str]:
    return {key: spreadsheet_safe_cell(value) for key, value in row.items()}


def ledger_export_id(ledger_path: Path) -> str:
    lineage_digest = hashlib.sha256(ledger_lineage_id(ledger_path).encode("ascii")).hexdigest()
    return f"{LEDGER_EXPORT_VERSION}:{lineage_digest}"


def reject_unsafe_csv_overwrite(
    path: Path,
    ledger_path: Path,
) -> None:
    if not path.exists():
        return

    expected_id = ledger_export_id(ledger_path)
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if LEDGER_EXPORT_FIELD not in (reader.fieldnames or ()):
                raise ValueError
            first_row = next(reader, None)
            if first_row is None or first_row.get(LEDGER_EXPORT_FIELD) != expected_id:
                raise ValueError
            if not all(row.get(LEDGER_EXPORT_FIELD) == expected_id for row in reader):
                raise ValueError
    except (OSError, UnicodeError, csv.Error, ValueError) as exc:
        raise ValueError(
            "Refusing to overwrite an existing CSV that was not exported by this ledger; "
            "move the CSV or migrate it before exporting."
        ) from exc


def export_balances_csv(
    path: Path,
    records: Iterable[BalanceRecord],
    *,
    ledger_path: Path,
) -> int:
    records = list(records)
    if not records:
        if path.exists():
            raise ValueError(
                "Refusing to overwrite an existing CSV without corresponding ledger history; "
                "move the CSV or restore the ledger before exporting."
            )
        raise ValueError("Cannot export balances: the ledger has no balance records.")
    ensure_private_dir(path.parent)
    lock_path = path.with_name(f".{path.name}.lock")
    with private_file_lock(lock_path):
        return _export_balances_csv_locked(path, records, ledger_path=ledger_path)


def _export_balances_csv_locked(
    path: Path,
    records: list[BalanceRecord],
    *,
    ledger_path: Path,
) -> int:
    reject_unsafe_csv_overwrite(path, ledger_path)
    fieldnames = [
        "date",
        "source",
        "balance_date",
        "balance",
        "iban",
        "bank_code",
        "account_number",
        "exported_at",
        LEDGER_EXPORT_FIELD,
    ]
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            count = 0
            export_id = ledger_export_id(ledger_path)
            for record in records:
                row = _balance_row(record)
                row[LEDGER_EXPORT_FIELD] = export_id
                writer.writerow(spreadsheet_safe_row(row))
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        ensure_private_file(temporary_path)
        os.replace(temporary_path, path)
        fsync_parent_directory(path)
        return count
    finally:
        temporary_path.unlink(missing_ok=True)


def _balance_row(record: BalanceRecord) -> dict[str, str]:
    return {
        "date": record.snapshot_date,
        "source": record.provenance.source,
        "balance_date": record.balance_date,
        "balance": record.amount,
        "iban": record.account_identifiers.get("iban", ""),
        "bank_code": record.account_identifiers.get("bank_code", ""),
        "account_number": record.account_identifiers.get("account_number", ""),
        "exported_at": record.provenance.to_dict()["imported_at"],
    }
