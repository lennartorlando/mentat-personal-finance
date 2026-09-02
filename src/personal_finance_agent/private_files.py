"""Private POSIX filesystem helpers shared by runtime data stores."""

from __future__ import annotations

import errno
import os
from pathlib import Path


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
