import os
import stat
import tempfile
import unittest
from pathlib import Path

from cptr.utils.runtime import FileError, _write_private_file


class PrivateSecretFileTests(unittest.TestCase):
    def test_private_write_creates_0600_without_returning_secret_metadata(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / ".env"
            result = _write_private_file(str(target), "PASSWORD=synthetic-test-secret\n", False)

            self.assertEqual(target.read_text(), "PASSWORD=synthetic-test-secret\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(result, {"status": "saved", "path": str(target)})
            self.assertNotIn("synthetic-test-secret", repr(result))

    def test_private_write_refuses_existing_file_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "secret.txt"
            target.write_text("old")
            with self.assertRaises(FileError) as denied:
                _write_private_file(str(target), "new-secret", False)

            self.assertEqual(denied.exception.status_code, 409)
            self.assertEqual(target.read_text(), "old")

    def test_private_write_refuses_symlink_target_even_with_overwrite(self):
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW is unavailable on this platform")
        with tempfile.TemporaryDirectory() as root:
            outside = Path(root) / "outside.txt"
            outside.write_text("do-not-touch")
            target = Path(root) / "secret-link"
            target.symlink_to(outside)

            with self.assertRaises(FileError) as denied:
                _write_private_file(str(target), "replacement-secret", True)

            self.assertEqual(denied.exception.status_code, 409)
            self.assertEqual(outside.read_text(), "do-not-touch")


if __name__ == "__main__":
    unittest.main()
