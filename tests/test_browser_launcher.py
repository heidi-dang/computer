import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cptr.utils.browser.launcher import (
    _managed_browser_startup_timeout_seconds,
    find_browser,
)


class ManagedBrowserStartupPolicyTests(unittest.TestCase):
    def test_default_startup_deadline_tolerates_slow_ci_browser_boot(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CPTR_MANAGED_BROWSER_STARTUP_TIMEOUT_SECONDS", None)
            self.assertEqual(_managed_browser_startup_timeout_seconds(), 20.0)

    def test_startup_deadline_is_configurable_but_bounded(self):
        for raw, expected in (("12.5", 12.5), ("1", 5.0), ("999", 60.0), ("invalid", 20.0)):
            with (
                self.subTest(raw=raw),
                patch.dict(
                    os.environ,
                    {"CPTR_MANAGED_BROWSER_STARTUP_TIMEOUT_SECONDS": raw},
                ),
            ):
                self.assertEqual(_managed_browser_startup_timeout_seconds(), expected)


class BrowserExecutableOverrideTests(unittest.TestCase):
    def _executable(self, root: str) -> Path:
        path = Path(root) / "chrome"
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    def test_absolute_configured_executable_is_preferred(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self._executable(tmp)
            with patch.dict(os.environ, {"CPTR_BROWSER_EXECUTABLE": str(executable)}, clear=False):
                self.assertEqual(find_browser(), str(executable.resolve()))

    def test_relative_configured_executable_fails_closed(self):
        with patch.dict(os.environ, {"CPTR_BROWSER_EXECUTABLE": "relative/chrome"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "must be an absolute path"):
                find_browser()

    def test_missing_configured_executable_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-chrome"
            with patch.dict(os.environ, {"CPTR_BROWSER_EXECUTABLE": str(missing)}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "does not exist"):
                    find_browser()

    def test_symlink_configured_executable_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            executable = self._executable(tmp)
            link = Path(tmp) / "chrome-link"
            link.symlink_to(executable)
            with patch.dict(os.environ, {"CPTR_BROWSER_EXECUTABLE": str(link)}, clear=False):
                with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
                    find_browser()


if __name__ == "__main__":
    unittest.main()
