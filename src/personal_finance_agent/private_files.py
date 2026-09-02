"""Private POSIX filesystem helpers shared by runtime data stores."""

from __future__ import annotations

import os
from pathlib import Path


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def chmod_private(path: Path, mode: int) -> None:
    if os.name == "posix":
        os.chmod(path, mode)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    chmod_private(path, PRIVATE_DIR_MODE)


def ensure_private_file(path: Path) -> None:
    if path.exists():
        chmod_private(path, PRIVATE_FILE_MODE)
