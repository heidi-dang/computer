import subprocess
import tempfile
import unittest
from pathlib import Path

from cptr.flowdeck.git_readonly import (
    GitInspectionPolicyError,
    GitInspectionRequest,
    inspect_git,
)


class ReadOnlyGitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "file.txt").write_text("one\n")
        subprocess.run(["git", "-C", str(self.root), "add", "file.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.name=Test", "-c", "user.email=test@example.com",
             "commit", "-qm", "initial"],
            check=True,
        )

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_fixed_operations_are_read_only_and_bounded(self):
        before = subprocess.check_output(["git", "-C", str(self.root), "status", "--porcelain"])
        status = await inspect_git(GitInspectionRequest("status", str(self.root)))
        log = await inspect_git(GitInspectionRequest("log", str(self.root), limit=1000))
        diff = await inspect_git(GitInspectionRequest("diff_stat", str(self.root)))
        self.assertIn("##", status)
        self.assertIn("initial", log)
        self.assertEqual(diff, "")
        after = subprocess.check_output(["git", "-C", str(self.root), "status", "--porcelain"])
        self.assertEqual(before, after)

    async def test_unsupported_or_out_of_scope_requests_are_denied(self):
        with self.assertRaises(GitInspectionPolicyError):
            await inspect_git(GitInspectionRequest("commit", str(self.root)))
        with self.assertRaises(GitInspectionPolicyError):
            await inspect_git(GitInspectionRequest("status", str(self.root), limit=0))
        with self.assertRaises(GitInspectionPolicyError):
            await inspect_git(GitInspectionRequest("status", str(self.root / "missing")))


if __name__ == "__main__":
    unittest.main()