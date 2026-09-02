import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from personal_finance_agent.aqbanking import (
    AqContext,
    append_csv,
    base,
    resolve_aq_tool,
    resolve_runtime_path,
    temporary_pinfile,
    user_config_path,
)


def write_user_config(ctx: AqContext, unique_user_id: str, user_id: str) -> None:
    path = user_config_path(ctx, unique_user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'char bankCode="12345678"\nchar userId="{user_id}"\n',
        encoding="utf-8",
    )


class TemporaryPinfileTests(unittest.TestCase):
    def test_supported_user_id_writes_pinfile_without_parser_breaking_whitespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = AqContext(Path(tmp))
            write_user_config(ctx, "1", "LoginNoSpace")

            with mock.patch("getpass.getpass", return_value="pin123"):
                with temporary_pinfile(ctx, "1") as pinfile:
                    content = pinfile.read_text(encoding="utf-8")

            self.assertIn('PIN_12345678_LoginNoSpace="pin123"', content)
            self.assertNotIn('PIN_12345678_LoginNoSpace = "pin123"', content)

    def test_space_containing_user_id_fails_before_pin_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = AqContext(Path(tmp))
            write_user_config(ctx, "1", "Login With Space")

            with mock.patch("getpass.getpass", side_effect=AssertionError("PIN prompt should not run")):
                with self.assertRaisesRegex(ValueError, "whitespace"):
                    with temporary_pinfile(ctx, "1"):
                        pass


class AqToolResolutionTests(unittest.TestCase):
    def test_explicit_executable_path_is_used_in_base_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "aqbanking-cli"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o700)

            ctx = AqContext(Path(tmp) / "runtime")
            command = base(ctx, "aqbanking-cli", tool)

            self.assertEqual(command[0], str(tool.resolve()))
            self.assertIn(f"--cfgdir={ctx.cfg_dir}", command)

    def test_missing_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_aq_tool("definitely-not-a-real-aqbanking-tool")

    def test_non_executable_tool_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "aqbanking-cli"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                resolve_aq_tool(tool)

    @unittest.skipUnless(os.name == "posix", "POSIX sticky-bit semantics only")
    def test_tool_under_sticky_world_writable_parent_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            sticky_dir = Path(tmp) / "sticky"
            sticky_dir.mkdir(mode=0o1777)
            sticky_dir.chmod(0o1777)
            private_dir = sticky_dir / "private"
            private_dir.mkdir(mode=0o700)
            tool = private_dir / "aqbanking-cli"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o700)

            self.assertEqual(resolve_aq_tool(tool), tool.resolve())

    @unittest.skipUnless(os.name == "posix", "POSIX permission semantics only")
    def test_tool_under_unprotected_world_writable_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            unsafe_dir = Path(tmp) / "unsafe"
            unsafe_dir.mkdir(mode=0o777)
            unsafe_dir.chmod(0o777)
            tool = unsafe_dir / "aqbanking-cli"
            tool.write_text("#!/bin/sh\n", encoding="utf-8")
            tool.chmod(0o700)

            with self.assertRaisesRegex(ValueError, "world-writable"):
                resolve_aq_tool(tool)


class RuntimePathTests(unittest.TestCase):
    def test_default_context_path_uses_runtime_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = AqContext(Path(tmp).resolve())

            self.assertEqual(resolve_runtime_path(ctx, None, ctx.context_file, False, "context"), ctx.data_dir / "aqbanking.ctx")

    def test_relative_paths_are_rooted_under_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = AqContext(Path(tmp).resolve())

            self.assertEqual(
                resolve_runtime_path(ctx, Path("reports/balances.csv"), ctx.data_dir / "balances.csv", False, "CSV export"),
                ctx.data_dir / "reports" / "balances.csv",
            )

    def test_external_absolute_path_requires_override(self):
        with tempfile.TemporaryDirectory() as runtime, tempfile.TemporaryDirectory() as outside:
            ctx = AqContext(Path(runtime).resolve())
            external = Path(outside).resolve() / "balances.csv"

            with self.assertRaisesRegex(ValueError, "outside"):
                resolve_runtime_path(ctx, external, ctx.data_dir / "balances.csv", False, "CSV export")

            self.assertEqual(
                resolve_runtime_path(ctx, external, ctx.data_dir / "balances.csv", True, "CSV export"),
                external,
            )


class CsvSafetyTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "posix", "POSIX permissions only")
    def test_append_csv_creates_private_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "balances.csv"
            with mock.patch("builtins.print"):
                append_csv(path, [{"date": "2026-07-02", "balance": "1.00 EUR"}])

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()
