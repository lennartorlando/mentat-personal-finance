"""Command line interface for finlocal."""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import json
import os
import sys
from pathlib import Path

from . import __version__
from .aqbanking import (
    AqContext,
    base,
    ensure_private_file,
    parse_balance_output,
    resolve_runtime_path,
    run_capture,
    run_interactive,
    run_with_optional_pinfile,
)
from .connectors.csv_import import parse_csv
from .connectors.csv_profile import CsvProfileError, ImportPeriod, load_csv_profile
from .ledger import (
    BalanceRecord,
    Diagnostic,
    JsonlLedger,
    LedgerValidationError,
    Provenance,
    account_record,
    balance_record_from_row,
    export_balances_csv,
    reject_unsafe_csv_overwrite,
    transaction_records,
)
from .security import validate_fints_pin


def context(args: argparse.Namespace) -> AqContext:
    return AqContext(root=Path(args.root).resolve())


def cmd_version(_: argparse.Namespace) -> int:
    print(f"personal-finance-agent {__version__}")
    return 0


def cmd_validate_pin(_: argparse.Namespace) -> int:
    import getpass

    pin = getpass.getpass("FinTS PIN to validate locally: ")
    validate_fints_pin(pin)
    print("PIN shape is valid for FinTS/AqBanking.")
    return 0


def cmd_aq_versions(args: argparse.Namespace) -> int:
    ctx = context(args)
    return run_interactive(ctx, base(ctx, "aqbanking-cli", args.aqbanking_cli) + ["versions"])


def cmd_aq_list_users(args: argparse.Namespace) -> int:
    ctx = context(args)
    return run_interactive(ctx, base(ctx, "aqhbci-tool4", args.aqhbci_tool4) + ["listusers"])


def cmd_aq_add_pintan_user(args: argparse.Namespace) -> int:
    ctx = context(args)
    command = base(ctx, "aqhbci-tool4", args.aqhbci_tool4) + [
        "adduser",
        "--tokentype=pintan",
        f"--bank={args.bank_code}",
        f"--user={args.user_id}",
        f"--server={args.server_url}",
    ]
    if args.customer_id:
        command.append(f"--customer={args.customer_id}")
    if args.username:
        command.append(f"--username={args.username}")
    return run_interactive(ctx, command)


def cmd_aq_get_accounts(args: argparse.Namespace) -> int:
    ctx = context(args)
    command = base(ctx, "aqhbci-tool4", args.aqhbci_tool4) + ["getaccounts"]
    if args.user:
        command.append(f"--user={args.user}")
    return run_with_optional_pinfile(ctx, command, args.safe_pin_user)


def cmd_aq_list_accounts(args: argparse.Namespace) -> int:
    ctx = context(args)
    return run_interactive(ctx, base(ctx, "aqbanking-cli", args.aqbanking_cli) + ["listaccs"])


def cmd_aq_balances(args: argparse.Namespace) -> int:
    ctx = context(args)
    context_file = resolve_runtime_path(ctx, args.context, ctx.context_file, args.allow_outside_runtime, "AqBanking context")
    csv_path = resolve_runtime_path(ctx, args.csv, ctx.data_dir / "balances.csv", args.allow_outside_runtime, "CSV export")
    ledger_path = resolve_runtime_path(
        ctx,
        args.ledger,
        ctx.data_dir / "ledger.jsonl",
        args.allow_outside_runtime,
        "Ledger",
    )
    _reject_same_ledger_and_csv_path(ledger_path, csv_path)
    if csv_path.exists():
        if not JsonlLedger(ledger_path).records("balance"):
            raise ValueError(
                "Refusing to overwrite an existing CSV without corresponding ledger history; "
                "move the CSV or restore the ledger before exporting."
            )
        reject_unsafe_csv_overwrite(csv_path, ledger_path)
    request_command = base(ctx, "aqbanking-cli", args.aqbanking_cli) + [
        "request",
        "--balance",
        f"--ctxfile={context_file}",
        "--ignoreUnsupported",
    ]
    if args.iban:
        request_command.append(f"--iban={args.iban}")
    code = run_with_optional_pinfile(ctx, request_command, args.safe_pin_user)
    if code != 0:
        return code

    template = "$(dateAsString)\t$(valueAsString)\t$(iban)\t$(bankcode)\t$(accountnumber)"
    ensure_private_file(context_file)
    output = run_capture(
        ctx,
        base(ctx, "aqbanking-cli", args.aqbanking_cli) + ["listbal", f"--ctxfile={context_file}", f"--template={template}"],
    )
    parsed = parse_balance_output(output, args.date)
    diagnostics = list(parsed.diagnostics)
    records = []
    for parsed_row in parsed.rows:
        try:
            records.append(
                balance_record_from_row(parsed_row.values, line=parsed_row.line)
            )
        except LedgerValidationError as exc:
            diagnostics.extend(exc.diagnostics)

    if not records:
        if not diagnostics:
            diagnostics.append(
                Diagnostic("aqbanking:listbal", "record", "no valid balance records were returned")
            )
        _print_diagnostics(diagnostics)
        return 2

    ledger = JsonlLedger(ledger_path)
    with ledger.locked() as session:
        result = session.add(records)
        balances = [
            record for record in result.records if isinstance(record, BalanceRecord)
        ]
        exported = export_balances_csv(csv_path, balances, ledger_path=ledger_path)
    print(
        f"Ledger import: {result.added} added, {result.updated} updated, "
        f"{result.duplicates} duplicate(s); "
        f"exported {exported} balance row(s) to {csv_path}."
    )
    _print_diagnostics(diagnostics + list(result.diagnostics))
    return 2 if diagnostics else 0


def cmd_ledger_inspect(args: argparse.Namespace) -> int:
    ctx = context(args)
    path = resolve_runtime_path(ctx, args.ledger, ctx.data_dir / "ledger.jsonl", args.allow_outside_runtime, "Ledger")
    records = JsonlLedger(path).records(args.record_type)
    if args.json:
        print(json.dumps({"count": len(records), "records": [record.to_dict() for record in records]}, sort_keys=True))
    else:
        counts: dict[str, int] = {}
        for record in records:
            counts[record.record_type] = counts.get(record.record_type, 0) + 1
        print(f"Ledger: {path}")
        print(f"Records: {len(records)}")
        for record_type, count in sorted(counts.items()):
            print(f"  {record_type}: {count}")
    return 0


def cmd_ledger_export(args: argparse.Namespace) -> int:
    ctx = context(args)
    ledger_path = resolve_runtime_path(
        ctx, args.ledger, ctx.data_dir / "ledger.jsonl", args.allow_outside_runtime, "Ledger"
    )
    csv_path = resolve_runtime_path(
        ctx, args.csv, ctx.data_dir / "balances.csv", args.allow_outside_runtime, "CSV export"
    )
    _reject_same_ledger_and_csv_path(ledger_path, csv_path)
    with JsonlLedger(ledger_path).locked() as session:
        balances = session.records("balance")
        exported = export_balances_csv(csv_path, balances, ledger_path=ledger_path)
    print(f"Exported {exported} balance row(s) from {ledger_path} to {csv_path}.")
    return 0


def cmd_ledger_import_csv(args: argparse.Namespace) -> int:
    ctx = context(args)
    input_path = Path(args.input).expanduser().resolve()
    profile_path = Path(args.profile).expanduser().resolve()
    ledger_path = resolve_runtime_path(
        ctx, args.ledger, ctx.data_dir / "ledger.jsonl", args.allow_outside_runtime, "Ledger"
    )
    protected_paths = (
        ledger_path,
        ledger_path.with_name(f".{ledger_path.name}.lock"),
        ctx.data_dir / "balances.csv",
    )
    _reject_unsafe_import_file(input_path, protected_paths, "CSV input")
    _reject_unsafe_import_file(profile_path, protected_paths, "CSV profile", check_export=False)
    if _paths_refer_to_same_file(input_path, profile_path):
        raise ValueError("Refusing CSV import: input and profile must refer to different files.")

    ensure_private_file(profile_path)
    try:
        profile = load_csv_profile(profile_path)
    except CsvProfileError as exc:
        _print_diagnostics(list(exc.diagnostics))
        return 2
    period = _csv_import_period(args.period, profile.period)
    if period is None:
        _print_diagnostics(
            [Diagnostic("csv-profile", "period", "must be declared in the profile or with --period")]
        )
        return 2

    parsed = parse_csv(input_path, profile)
    if parsed.diagnostics:
        _print_diagnostics(list(parsed.diagnostics))
        return 2

    if args.preview:
        print(
            json.dumps(
                {
                    "period": {"start": period.start, "end": period.end},
                    "rows": parsed.rows,
                    "source_ref": profile.source_ref,
                },
                sort_keys=True,
            )
        )
        return 0

    provenance = Provenance(profile.source, dt.datetime.now(dt.timezone.utc), profile.source_ref)
    account = account_record(
        profile.account.identifier,
        provenance,
        name=profile.account.name,
        currency=profile.account.currency,
    )
    try:
        incoming = transaction_records(
            (dict(row, account_id=account.id) for row in parsed.rows),
            provenance,
            complete_source_slice=True,
            input_name="csv-import",
        )
    except LedgerValidationError as exc:
        _print_diagnostics(list(exc.diagnostics))
        return 2

    ledger = JsonlLedger(ledger_path)
    with ledger.locked() as session:
        incoming_ids = {record.id for record in incoming}
        for existing in session.records("transaction"):
            if (
                existing.id in incoming_ids
                and (existing.provenance.source, existing.provenance.source_ref)
                != (provenance.source, provenance.source_ref)
            ):
                raise ValueError(
                    "transaction identity already belongs to a different source slice"
                )
        account_result = session.add([account])
        retained = [
            record
            for record in account_result.records
            if record.record_type == "transaction"
            and (record.provenance.source, record.provenance.source_ref)
            == (profile.source, profile.source_ref)
            and not (period.start <= record.booking_date <= period.end)
        ]
        result = session.replace_transaction_slice(itertools.chain(retained, incoming), provenance)

    print(
        f"CSV import: {result.added} added, {result.updated} updated, "
        f"{result.duplicates} duplicate(s), {result.removed} retired; "
        f"source_ref={profile.source_ref}."
    )
    _print_diagnostics(list(result.diagnostics))
    return 0


def _csv_import_period(
    override: list[str] | None, declared: ImportPeriod | None
) -> ImportPeriod | None:
    if override is None:
        return declared
    normalized = []
    for value in override:
        try:
            parsed = dt.datetime.strptime(value, "%Y-%m-%d").date().isoformat()
        except ValueError as exc:
            raise ValueError("CSV import period must use YYYY-MM-DD.") from exc
        normalized.append(parsed)
    if normalized[0] > normalized[1]:
        raise ValueError("CSV import period start must not be after end.")
    return ImportPeriod(normalized[0], normalized[1])


def _reject_unsafe_import_file(
    path: Path,
    protected_paths: tuple[Path, ...],
    label: str,
    *,
    check_export: bool = True,
) -> None:
    if any(_paths_refer_to_same_file(path, item.resolve()) for item in protected_paths):
        raise ValueError(f"Refusing {label}: path is reserved for Mentat runtime data.")
    if not path.is_file():
        raise ValueError(f"Refusing {label}: path is not a regular file.")
    if not check_export:
        return
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(65537)
    except OSError as exc:
        raise ValueError(f"Refusing {label}: file could not be read.") from exc
    if b"ledger_export_id" in first_line:
        raise ValueError(f"Refusing {label}: Mentat-exported files cannot be imported.")


def _reject_same_ledger_and_csv_path(ledger_path: Path, csv_path: Path) -> None:
    if _paths_refer_to_same_file(ledger_path, csv_path):
        raise ValueError("Ledger and CSV export paths must refer to different files.")


def _paths_refer_to_same_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        if left.samefile(right):
            return True
    except FileNotFoundError:
        pass
    left_key = os.path.normcase(os.fspath(left)).casefold()
    right_key = os.path.normcase(os.fspath(right)).casefold()
    return left_key == right_key and (
        _filesystem_is_case_insensitive(left)
        or _filesystem_is_case_insensitive(right)
    )


def _filesystem_is_case_insensitive(path: Path) -> bool:
    if os.name == "nt":
        return True
    for ancestor in path.parents:
        if not ancestor.exists() or not ancestor.name:
            continue
        case_variant = ancestor.with_name(ancestor.name.swapcase())
        if case_variant == ancestor:
            continue
        try:
            return ancestor.samefile(case_variant)
        except FileNotFoundError:
            return False
    return False


def _print_diagnostics(diagnostics: list[Diagnostic]) -> None:
    for diagnostic in diagnostics:
        print(json.dumps({"diagnostic": diagnostic.to_dict()}, sort_keys=True), file=sys.stderr)


def add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="Project runtime root. Default: current directory.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local-first personal finance CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    version = subparsers.add_parser("version")
    version.set_defaults(func=cmd_version)

    validate_pin = subparsers.add_parser("validate-pin")
    validate_pin.set_defaults(func=cmd_validate_pin)

    aq = subparsers.add_parser("aq", help="AqBanking workflows.")
    aq.add_argument("--aqbanking-cli", help="Explicit aqbanking-cli executable path.")
    aq.add_argument("--aqhbci-tool4", help="Explicit aqhbci-tool4 executable path.")
    aq_sub = aq.add_subparsers(dest="aq_command", required=True)

    aq_versions = aq_sub.add_parser("versions")
    add_root(aq_versions)
    aq_versions.set_defaults(func=cmd_aq_versions)

    aq_users = aq_sub.add_parser("list-users")
    add_root(aq_users)
    aq_users.set_defaults(func=cmd_aq_list_users)

    aq_add = aq_sub.add_parser("add-pintan-user")
    add_root(aq_add)
    aq_add.add_argument("--bank-code", required=True)
    aq_add.add_argument("--user-id", required=True)
    aq_add.add_argument("--server-url", required=True)
    aq_add.add_argument("--customer-id")
    aq_add.add_argument("--username")
    aq_add.set_defaults(func=cmd_aq_add_pintan_user)

    aq_get_accounts = aq_sub.add_parser("get-accounts")
    add_root(aq_get_accounts)
    aq_get_accounts.add_argument("--user")
    aq_get_accounts.add_argument("--safe-pin-user")
    aq_get_accounts.set_defaults(func=cmd_aq_get_accounts)

    aq_list_accounts = aq_sub.add_parser("list-accounts")
    add_root(aq_list_accounts)
    aq_list_accounts.set_defaults(func=cmd_aq_list_accounts)

    aq_balances = aq_sub.add_parser("balances")
    add_root(aq_balances)
    aq_balances.add_argument("--context", type=Path)
    aq_balances.add_argument("--csv", type=Path)
    aq_balances.add_argument("--ledger", type=Path)
    aq_balances.add_argument("--date", default=dt.date.today().isoformat())
    aq_balances.add_argument("--iban")
    aq_balances.add_argument("--safe-pin-user")
    aq_balances.add_argument("--allow-outside-runtime", action="store_true")
    aq_balances.set_defaults(func=cmd_aq_balances)

    ledger = subparsers.add_parser("ledger", help="Inspect and export the local JSONL ledger.")
    ledger_sub = ledger.add_subparsers(dest="ledger_command", required=True)

    ledger_inspect = ledger_sub.add_parser("inspect", help="Inspect normalized ledger records.")
    add_root(ledger_inspect)
    ledger_inspect.add_argument("--ledger", type=Path)
    ledger_inspect.add_argument(
        "--record-type", choices=("account", "balance", "transaction", "holding")
    )
    ledger_inspect.add_argument("--json", action="store_true", help="Emit one machine-readable JSON document.")
    ledger_inspect.add_argument("--allow-outside-runtime", action="store_true")
    ledger_inspect.set_defaults(func=cmd_ledger_inspect)

    ledger_export = ledger_sub.add_parser("export", help="Export ledger balances to CSV.")
    add_root(ledger_export)
    ledger_export.add_argument("--ledger", type=Path)
    ledger_export.add_argument("--csv", type=Path)
    ledger_export.add_argument("--allow-outside-runtime", action="store_true")
    ledger_export.set_defaults(func=cmd_ledger_export)

    ledger_import = ledger_sub.add_parser("import-csv", help="Import a declared-format CSV export.")
    add_root(ledger_import)
    ledger_import.add_argument("--input", type=Path, required=True)
    ledger_import.add_argument("--profile", type=Path, required=True)
    ledger_import.add_argument("--ledger", type=Path)
    ledger_import.add_argument(
        "--period", nargs=2, metavar=("START", "END"), help="Override the profile period (YYYY-MM-DD)."
    )
    ledger_import.add_argument("--preview", action="store_true")
    ledger_import.add_argument("--allow-outside-runtime", action="store_true")
    ledger_import.set_defaults(func=cmd_ledger_import_csv)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
