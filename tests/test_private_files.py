import errno
import os
import unittest
from pathlib import Path
from unittest import mock

from personal_finance_agent.private_files import fsync_parent_directory


@unittest.skipUnless(os.name == "posix", "directory fsync is POSIX-specific")
class DirectoryFsyncTests(unittest.TestCase):
    def test_unsupported_directory_fsync_errors_are_ignored(self):
        unsupported_errors = {
            error_number
            for error_name in ("EINVAL", "ENOTSUP", "EOPNOTSUPP")
            if (error_number := getattr(errno, error_name, None)) is not None
        }

        for error_number in unsupported_errors:
            with self.subTest(error_number=error_number), mock.patch(
                "personal_finance_agent.private_files.os.open", return_value=42
            ), mock.patch(
                "personal_finance_agent.private_files.os.fsync",
                side_effect=OSError(error_number, "unsupported"),
            ) as fsync, mock.patch(
                "personal_finance_agent.private_files.os.close"
            ) as close:
                fsync_parent_directory(Path("/runtime/data/ledger.jsonl"))

            fsync.assert_called_once_with(42)
            close.assert_called_once_with(42)

    def test_directory_fsync_reraises_real_io_failure(self):
        with mock.patch(
            "personal_finance_agent.private_files.os.open", return_value=42
        ), mock.patch(
            "personal_finance_agent.private_files.os.fsync",
            side_effect=OSError(errno.EIO, "I/O failure"),
        ), mock.patch("personal_finance_agent.private_files.os.close") as close:
            with self.assertRaisesRegex(OSError, "I/O failure"):
                fsync_parent_directory(Path("/runtime/data/ledger.jsonl"))

        close.assert_called_once_with(42)


if __name__ == "__main__":
    unittest.main()
