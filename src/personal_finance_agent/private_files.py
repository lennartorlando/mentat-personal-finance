"""Private POSIX filesystem helpers shared by runtime data stores."""

from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

if os.name == "posix":
    import fcntl
elif os.name == "nt":
    import msvcrt


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = frozenset(
    error_number
    for error_name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP")
    if (error_number := getattr(errno, error_name, None)) is not None
)


def chmod_private(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    chmod_private(path, PRIVATE_DIR_MODE)


def ensure_private_file(path: Path) -> None:
    if path.exists():
        chmod_private(path, PRIVATE_FILE_MODE)


def fsync_parent_directory(path: Path) -> None:
    """Make a completed rename durable on filesystems that support directory fsync."""
    if os.name != "posix":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path.parent, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
                raise
    finally:
        os.close(descriptor)


@contextmanager
def private_file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock in a private, durable lock file."""
    ensure_private_dir(lock_path.parent)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, PRIVATE_FILE_MODE)
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
            raise OSError(f"File locking is unsupported on platform {os.name!r}")
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
