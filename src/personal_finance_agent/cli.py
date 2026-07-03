"""Command line interface for finlocal."""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import __version__
from .aqbanking import (
    AqContext,
    append_csv,
    base,
    ensure_private_file,
    parse_balance_output,
    resolve_runtime_path,
    run_capture,
    run_interactive,
    run_with_optional_pinfile,
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
    rows = parse_balance_output(output, args.date)
    append_csv(csv_path, rows)
    return 0


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
    aq_balances.add_argument("--date", default=dt.date.today().isoformat())
    aq_balances.add_argument("--iban")
    aq_balances.add_argument("--safe-pin-user")
    aq_balances.add_argument("--allow-outside-runtime", action="store_true")
    aq_balances.set_defaults(func=cmd_aq_balances)

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
