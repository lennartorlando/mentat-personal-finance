"""Private JSONL storage for normalized ledger records.

All record types intentionally share one hand-inspectable file. Each line is a
self-contained record with a ``record_type`` discriminator; keeping one stream
also makes cross-type identity checks and atomic rewrites a single operation.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

if os.name == "posix":
    import fcntl
elif os.name == "nt":
    import msvcrt

from ..private_files import ensure_private_dir, ensure_private_file
from .models import BalanceRecord, LedgerRecord, record_from_dict


@dataclass(frozen=True)
class AddResult:
    added: int
    duplicates: int
    records: tuple[LedgerRecord, ...]
    updated: int = 0


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

    def _replace_records(self, records: Iterable[LedgerRecord]) -> None:
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
        finally:
            temporary_path.unlink(missing_ok=True)


class _LockedLedger:
    """Ledger operations performed under one already-acquired writer lock."""

    def __init__(self, ledger: JsonlLedger):
        self._ledger = ledger

    def records(self, record_type: str | None = None) -> list[LedgerRecord]:
        return self._ledger.records(record_type)

    def add(self, records: Iterable[LedgerRecord]) -> AddResult:
        existing_records = self.records()
        combined_records = list(existing_records)
        positions = {record.id: index for index, record in enumerate(combined_records)}
        added = 0
        duplicates = 0
        updated = 0
        for record in records:
            position = positions.get(record.id)
            if position is None:
                positions[record.id] = len(combined_records)
                combined_records.append(record)
                added += 1
                continue

            existing = combined_records[position]
            if (
                isinstance(existing, BalanceRecord)
                and isinstance(record, BalanceRecord)
                and not _same_balance_content(existing, record)
            ):
                combined_records[position] = record
                updated += 1
            else:
                duplicates += 1

        if not added and not updated:
            return AddResult(
                added=0,
                duplicates=duplicates,
                records=tuple(existing_records),
            )

        self._ledger._replace_records(combined_records)
        return AddResult(
            added=added,
            duplicates=duplicates,
            records=tuple(combined_records),
            updated=updated,
        )


def _same_balance_content(left: BalanceRecord, right: BalanceRecord) -> bool:
    """Compare persisted meaning while ignoring fetch-time provenance only."""
    left_value = left.to_dict()
    right_value = right.to_dict()
    left_value["provenance"].pop("imported_at")
    right_value["provenance"].pop("imported_at")
    return left_value == right_value


@contextmanager
def _writer_lock(ledger_path: Path) -> Iterator[None]:
    lock_path = ledger_path.with_name(f".{ledger_path.name}.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        ensure_private_file(lock_path)
        if os.name == "posix":
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        elif os.name == "nt":
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            raise OSError(f"Writer locking is unsupported on platform {os.name!r}")
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif os.name == "nt":
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    finally:
        os.close(descriptor)
