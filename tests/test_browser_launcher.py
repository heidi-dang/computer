import os
import unittest
from unittest.mock import patch

from cptr.utils.browser.launcher import _managed_browser_startup_timeout_seconds


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


if __name__ == "__main__":
    unittest.main()
