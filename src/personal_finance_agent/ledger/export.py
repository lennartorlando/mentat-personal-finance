"""Ledger export adapters. Spreadsheet neutralization happens only here."""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path
from typing import Iterable

from ..private_files import ensure_private_dir, ensure_private_file, fsync_parent_directory
from .models import BalanceRecord


SPREADSHEET_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def spreadsheet_safe_cell(value: str) -> str:
    if value.startswith(SPREADSHEET_FORMULA_PREFIXES):
        return "'" + value
    return value


def spreadsheet_safe_row(row: dict[str, str]) -> dict[str, str]:
    return {key: spreadsheet_safe_cell(value) for key, value in row.items()}


def export_balances_csv(path: Path, records: Iterable[BalanceRecord]) -> int:
    ensure_private_dir(path.parent)
    fieldnames = [
        "date",
        "source",
        "balance_date",
        "balance",
        "iban",
        "bank_code",
        "account_number",
        "exported_at",
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
            for record in records:
                writer.writerow(spreadsheet_safe_row(_balance_row(record)))
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
