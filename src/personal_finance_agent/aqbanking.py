"""AqBanking command runner and parsers."""

from __future__ import annotations

import csv
import datetime as dt
import getpass
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .ledger import export as ledger_export
from .ledger.models import Diagnostic
from .private_files import (
    PRIVATE_DIR_MODE,
    PRIVATE_FILE_MODE,
    chmod_private,
    ensure_private_dir,
    ensure_private_file,
)
from .security import validate_fints_pin


SPREADSHEET_FORMULA_PREFIXES = ledger_export.SPREADSHEET_FORMULA_PREFIXES
spreadsheet_safe_cell = ledger_export.spreadsheet_safe_cell
spreadsheet_safe_row = ledger_export.spreadsheet_safe_row
TRUSTED_TOOL_PREFIXES = (
    Path("/opt/homebrew").resolve(),
    Path("/usr/local").resolve(),
    Path("/usr").resolve(),
)


@dataclass(frozen=True)
class AqContext:
    root: Path

    @property
    def cfg_dir(self) -> Path:
        return self.root / "config" / "aqbanking.local"

    @property
    def home_dir(self) -> Path:
        return self.root / "config" / "aqbanking.home.local"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def context_file(self) -> Path:
        return self.data_dir / "aqbanking.ctx"

    def prepare(self) -> None:
        ensure_private_dir(self.cfg_dir)
        ensure_private_dir(self.home_dir)
        ensure_private_dir(self.data_dir)


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def is_unsafe_world_writable_dir(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
        return bool(mode & stat.S_IWOTH) and not bool(mode & stat.S_ISVTX)
    except OSError:
        return False


def has_world_writable_parent(path: Path) -> bool:
    resolved = path.resolve()
    for parent in (resolved.parent, *resolved.parents):
        if is_unsafe_world_writable_dir(parent):
            return True
    return False


def is_trusted_tool_path(path: Path) -> bool:
    resolved = path.resolve()
    return any(is_relative_to(resolved, prefix) for prefix in TRUSTED_TOOL_PREFIXES)


def resolve_aq_tool(tool: str | Path) -> Path:
    candidate = Path(tool).expanduser()
    explicit_path = candidate.is_absolute() or len(candidate.parts) > 1
    if explicit_path:
        resolved = candidate.resolve()
    else:
        found = shutil.which(str(tool))
        if not found:
            raise ValueError(f"Could not find AqBanking tool: {tool}")
        resolved = Path(found).resolve()
        if not is_trusted_tool_path(resolved):
            raise ValueError(f"Refusing PATH-resolved AqBanking tool outside trusted install locations: {tool}")

    if not resolved.is_file():
        raise ValueError(f"AqBanking tool is not a file: {resolved}")
    if not os.access(resolved, os.X_OK):
        raise ValueError(f"AqBanking tool is not executable: {resolved}")
    if has_world_writable_parent(resolved):
        raise ValueError(f"Refusing AqBanking tool from a world-writable path: {resolved}")
    return resolved


def aq_tool_override_name(tool: str) -> str:
    return "PFA_" + tool.upper().replace("-", "_")


def base(ctx: AqContext, tool: str, tool_path: str | Path | None = None) -> list[str]:
    override = tool_path or os.environ.get(aq_tool_override_name(tool))
    resolved_tool = resolve_aq_tool(override or tool)
    return [str(resolved_tool), f"--cfgdir={ctx.cfg_dir}", "--acceptvalidcerts"]


def aq_env(ctx: AqContext) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(ctx.home_dir)
    env["AQBANKING_LOGLEVEL"] = "error"
    env.pop("AQBANKING_STORE_JOBLOGS", None)
    return env


def user_config_path(ctx: AqContext, unique_user_id: str) -> Path:
    return ctx.cfg_dir / "settings6" / "users" / f"{int(unique_user_id):08d}.conf"


def read_aq_value(path: Path, key: str) -> str:
    pattern = re.compile(rf'^\w+\s+{re.escape(key)}="(.*)"$')
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line.strip())
        if match:
            return match.group(1)
    raise ValueError(f"Could not find {key} in {path}.")


def quote_pin_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def pinfile_key(bank_code: str, user_id: str) -> str:
    key = f"PIN_{bank_code}_{user_id}"
    if any(char.isspace() for char in key):
        raise ValueError("AqBanking PIN files do not support whitespace in the generated PIN key.")
    return key


@contextmanager
def temporary_pinfile(ctx: AqContext, unique_user_id: str) -> Iterator[Path]:
    path = user_config_path(ctx, unique_user_id)
    if not path.exists():
        raise ValueError(f"Unknown AqBanking user id: {unique_user_id}")
    bank_code = read_aq_value(path, "bankCode")
    user_id = read_aq_value(path, "userId")
    key = pinfile_key(bank_code, user_id)
    pin = getpass.getpass(f"PIN for AqBanking user {unique_user_id}: ")
    validate_fints_pin(pin)

    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False)
    pinfile = Path(handle.name)
    try:
        chmod_private(pinfile, PRIVATE_FILE_MODE)
        handle.write("# Temporary AqBanking PIN file generated by finlocal.\n")
        handle.write(f'{key}="{quote_pin_value(pin)}"\n')
        handle.close()
        yield pinfile
    finally:
        try:
            pinfile.unlink()
        except FileNotFoundError:
            pass


def run_interactive(ctx: AqContext, args: list[str]) -> int:
    ctx.prepare()
    return subprocess.run(args, env=aq_env(ctx), check=False).returncode


def run_capture(ctx: AqContext, args: list[str]) -> str:
    ctx.prepare()
    result = subprocess.run(args, env=aq_env(ctx), text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + "\n" + result.stderr).strip())
    return result.stdout


def run_with_optional_pinfile(ctx: AqContext, command: list[str], unique_user_id: str | None) -> int:
    if not unique_user_id:
        return run_interactive(ctx, command)
    with temporary_pinfile(ctx, unique_user_id) as pinfile:
        return run_interactive(ctx, command[:1] + [f"--pinfile={pinfile}", *command[1:]])


def resolve_runtime_path(
    ctx: AqContext,
    value: str | Path | None,
    default: Path,
    allow_outside: bool,
    label: str,
) -> Path:
    data_root = ctx.data_dir.resolve()
    if value is None:
        resolved = default.resolve()
    else:
        candidate = Path(value).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (ctx.data_dir / candidate).resolve()

    if not allow_outside and not is_relative_to(resolved, data_root):
        raise ValueError(f"{label} path is outside the runtime data directory; pass --allow-outside-runtime to opt in.")
    return resolved


@dataclass(frozen=True)
class BalanceParseResult:
    """The accepted rows and rejected-field diagnostics from one parser run."""

    rows: tuple[dict[str, str], ...]
    diagnostics: tuple[Diagnostic, ...]


def parse_balance_output(output: str, snapshot_date: str) -> BalanceParseResult:
    rows: list[dict[str, str]] = []
    diagnostics: list[Diagnostic] = []
    field_names = ("balance_date", "balance", "iban", "bank_code", "account_number")
    for line_number, line in enumerate(output.splitlines(), start=1):
        parts = line.split("\t")
        if len(parts) != 5:
            if len(parts) < len(field_names):
                diagnostics.extend(
                    Diagnostic("aqbanking:listbal", field, "field is missing", line_number)
                    for field in field_names[len(parts) :]
                )
            else:
                diagnostics.append(
                    Diagnostic(
                        "aqbanking:listbal",
                        "record",
                        f"expected 5 tab-separated fields, received {len(parts)}",
                        line_number,
                    )
                )
            continue
        balance_date, value, iban, bank_code, account_number = parts
        rows.append(
            {
                "date": snapshot_date,
                "source": "AqBanking",
                "balance_date": balance_date,
                "balance": value,
                "iban": iban,
                "bank_code": bank_code,
                "account_number": account_number,
                "exported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            }
        )
    return BalanceParseResult(tuple(rows), tuple(diagnostics))


def append_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        print("No rows exported.")
        return
    ensure_private_dir(path.parent)
    exists = path.exists()
    ensure_private_file(path)
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        if not exists:
            writer.writeheader()
        writer.writerows(spreadsheet_safe_row(row) for row in rows)
    ensure_private_file(path)
    print(f"Appended {len(rows)} row(s) to {path}.")
