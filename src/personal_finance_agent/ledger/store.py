"""Private JSONL storage for normalized ledger records.

All record types intentionally share one hand-inspectable file. Each line is a
self-contained record with a ``record_type`` discriminator; keeping one stream
also makes cross-type identity checks and atomic rewrites a single operation.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

from ..private_files import (
    ensure_private_dir,
    ensure_private_file,
    fsync_parent_directory,
    private_file_lock,
)
from .models import (
    BalanceRecord,
    Diagnostic,
    LedgerRecord,
    Provenance,
    TransactionRecord,
    canonical_balance_record,
    record_from_dict,
)


@dataclass(frozen=True)
class AddResult:
    added: int
    duplicates: int
    records: tuple[LedgerRecord, ...]
    updated: int = 0
    removed: int = 0
    diagnostics: tuple[Diagnostic, ...] = ()


class JsonlLedger:
    def __init__(self, path: Path):
        self.path = path

    def records(self, record_type: str | None = None) -> list[LedgerRecord]:
        try:
            handle = self.path.open(encoding="utf-8")
        except FileNotFoundError:
            return []
        ensure_private_file(self.path)
        records: list[LedgerRecord] = []
        with handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    record = record_from_dict(value)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(f"Invalid ledger record at {self.path}:{line_number}: {exc}") from exc
                if record_type is None or record.record_type == record_type:
                    records.append(record)
        return records

    @contextmanager
    def locked(self) -> Iterator[_LockedLedger]:
        """Coordinate a ledger operation and any publication derived from it.

        The yielded session exposes lock-aware ``add`` and ``records`` methods.
        Callers that publish a derived artifact should do so before leaving this
        context, ensuring the artifact reflects the latest serialized ledger.
        """
        ensure_private_dir(self.path.parent)
        with _writer_lock(self.path):
            yield _LockedLedger(self)

    def add(self, records: Iterable[LedgerRecord]) -> AddResult:
        with self.locked() as session:
            return session.add(records)

    def replace_transaction_slice(
        self,
        records: Iterable[TransactionRecord],
        provenance: Provenance,
    ) -> AddResult:
        with self.locked() as session:
            return session.replace_transaction_slice(records, provenance)

    def _replace_records(self, records: Iterable[LedgerRecord]) -> None:
        lineage_id = _valid_ledger_lineage(self.path)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                for record in records:
                    json.dump(
                        record.to_dict(),
                        handle,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            ensure_private_file(temporary_path)
            os.replace(temporary_path, self.path)
            ensure_private_file(self.path)
            fsync_parent_directory(self.path)
            _write_ledger_lineage(self.path, lineage_id or secrets.token_hex(32))
        finally:
            temporary_path.unlink(missing_ok=True)


class _LockedLedger:
    """Ledger operations performed under one already-acquired writer lock."""

    def __init__(self, ledger: JsonlLedger):
        self._ledger = ledger

    def records(self, record_type: str | None = None) -> list[LedgerRecord]:
        return self._ledger.records(record_type)

    def add(self, records: Iterable[LedgerRecord]) -> AddResult:
        return self._merge(records)

    def replace_transaction_slice(
        self,
        records: Iterable[TransactionRecord],
        provenance: Provenance,
    ) -> AddResult:
        source_slice = (provenance.source, provenance.source_ref)
        incoming_records = list(records)
        for record in incoming_records:
            if not isinstance(record, TransactionRecord):
                raise TypeError("transaction slices may contain only transaction records")
            if (record.provenance.source, record.provenance.source_ref) != source_slice:
                raise ValueError("transaction record provenance does not match the source slice")
        return self._merge(incoming_records, transaction_slice=source_slice)

    def _merge(
        self,
        records: Iterable[LedgerRecord],
        *,
        transaction_slice: tuple[str, str] | None = None,
    ) -> AddResult:
        incoming_records = [record_from_dict(record.to_dict()) for record in records]
        existing_records = self.records()
        canonical_existing_records = [
            canonical_balance_record(record) if isinstance(record, BalanceRecord) else record
            for record in existing_records
        ]
        migrated = canonical_existing_records != existing_records
        existing_records = canonical_existing_records
        if transaction_slice is not None:
            existing_by_id = {record.id: record for record in existing_records}
            for record in incoming_records:
                if not isinstance(record, TransactionRecord):
                    continue
                existing = existing_by_id.get(record.id)
                if (
                    isinstance(existing, TransactionRecord)
                    and (existing.provenance.source, existing.provenance.source_ref)
                    != transaction_slice
                ):
                    raise ValueError(
                        "transaction identity already belongs to a different source slice"
                    )
        incoming_transaction_ids = {
            record.id for record in incoming_records if isinstance(record, TransactionRecord)
        }
        combined_records = [
            record
            for record in existing_records
            if not (
                isinstance(record, TransactionRecord)
                and (record.provenance.source, record.provenance.source_ref) == transaction_slice
                and record.id not in incoming_transaction_ids
            )
        ]
        removed = len(existing_records) - len(combined_records)
        positions = {record.id: index for index, record in enumerate(combined_records)}
        added = 0
        duplicates = 0
        updated = 0
        diagnostics: list[Diagnostic] = []
        for record in incoming_records:
            position = positions.get(record.id)
            if position is None:
                positions[record.id] = len(combined_records)
                combined_records.append(record)
                added += 1
                continue

            existing = combined_records[position]
            if not _same_record_content(existing, record):
                combined_records[position] = record
                updated += 1
                if (
                    isinstance(existing, BalanceRecord)
                    and isinstance(record, BalanceRecord)
                    and existing.amount != record.amount
                ):
                    diagnostics.append(
                        Diagnostic(
                            record.provenance.source_ref,
                            "amount",
                            "balance amount changed",
                            record_id=record.id,
                            previous_value=existing.amount,
                            new_value=record.amount,
                        )
                    )
                if existing.provenance.source_ref != record.provenance.source_ref:
                    diagnostics.append(
                        Diagnostic(
                            record.provenance.source_ref,
                            "provenance.source_ref",
                            "record provenance changed",
                            record_id=record.id,
                            previous_value=existing.provenance.source_ref,
                            new_value=record.provenance.source_ref,
                        )
                    )
            else:
                duplicates += 1

        if not added and not updated and not removed and not migrated:
            return AddResult(
                added=0,
                duplicates=duplicates,
                records=tuple(existing_records),
                diagnostics=tuple(diagnostics),
            )

        self._ledger._replace_records(combined_records)
        return AddResult(
            added=added,
            duplicates=duplicates,
            records=tuple(combined_records),
            updated=updated,
            removed=removed,
            diagnostics=tuple(diagnostics),
        )


def _same_record_content(left: LedgerRecord, right: LedgerRecord) -> bool:
    """Compare persisted meaning while ignoring fetch-time provenance only."""
    left_value = left.to_dict()
    right_value = right.to_dict()
    left_value["provenance"].pop("imported_at")
    right_value["provenance"].pop("imported_at")
    return left_value == right_value


_LINEAGE_VERSION = 1


def ledger_lineage_id(ledger_path: Path) -> str:
    """Return the durable identity of this ledger incarnation."""
    lineage_id = _valid_ledger_lineage(ledger_path)
    if lineage_id is not None:
        return lineage_id
    lineage_id = secrets.token_hex(32)
    _write_ledger_lineage(ledger_path, lineage_id)
    return lineage_id


def _lineage_path(ledger_path: Path) -> Path:
    ledger_path = ledger_path.resolve(strict=False)
    return ledger_path.with_name(f".{ledger_path.name}.lineage")


def _ledger_fingerprint(ledger_path: Path) -> dict[str, object] | None:
    try:
        with ledger_path.open("rb") as handle:
            file_stat = os.fstat(handle.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            final_stat = os.fstat(handle.fileno())
    except FileNotFoundError:
        return None
    if (file_stat.st_dev, file_stat.st_ino, file_stat.st_size) != (
        final_stat.st_dev,
        final_stat.st_ino,
        final_stat.st_size,
    ):
        raise OSError("Ledger changed while its lineage was being read")
    return {
        "device": final_stat.st_dev,
        "inode": final_stat.st_ino,
        "sha256": digest.hexdigest(),
        "size": final_stat.st_size,
    }


def _valid_ledger_lineage(ledger_path: Path) -> str | None:
    lineage_path = _lineage_path(ledger_path)
    try:
        ensure_private_file(lineage_path)
        value = json.loads(lineage_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("version") != _LINEAGE_VERSION:
        return None
    lineage_id = value.get("lineage_id")
    if not isinstance(lineage_id, str) or not lineage_id:
        return None
    if value.get("ledger") != _ledger_fingerprint(ledger_path):
        return None
    return lineage_id


def _write_ledger_lineage(ledger_path: Path, lineage_id: str) -> None:
    lineage_path = _lineage_path(ledger_path)
    ensure_private_dir(lineage_path.parent)
    value = {
        "version": _LINEAGE_VERSION,
        "lineage_id": lineage_id,
        "ledger": _ledger_fingerprint(ledger_path),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        dir=lineage_path.parent,
        prefix=f".{lineage_path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        ensure_private_file(temporary_path)
        os.replace(temporary_path, lineage_path)
        ensure_private_file(lineage_path)
        fsync_parent_directory(lineage_path)
    finally:
        temporary_path.unlink(missing_ok=True)


@contextmanager
def _writer_lock(ledger_path: Path) -> Iterator[None]:
    lock_path = ledger_path.with_name(f".{ledger_path.name}.lock")
    with private_file_lock(lock_path):
        yield
