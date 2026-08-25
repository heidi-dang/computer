import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from cptr.utils.workspace_fingerprint import snapshot_workspace


class WorkspaceFingerprintTests(unittest.TestCase):
    def test_existing_untracked_content_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True
            )
            subprocess.run(["git", "config", "user.name", "CPTR Test"], cwd=root, check=True)
            (root / "tracked.txt").write_text("tracked\n", encoding="utf-8")
            subprocess.run(["git", "add", "tracked.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
            fixture = root / "existing-untracked.txt"
            fixture.write_text("base\n", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "ignored.js").write_text("ignored\n", encoding="utf-8")

            before = asyncio.run(snapshot_workspace(str(root)))
            fixture.write_text("base\nsteering\n", encoding="utf-8")
            after = asyncio.run(snapshot_workspace(str(root)))

            self.assertNotEqual(before["fingerprint"], after["fingerprint"])
            before_file = next(item for item in before["files"] if item["path"] == fixture.name)
            after_file = next(item for item in after["files"] if item["path"] == fixture.name)
            self.assertNotEqual(before_file["sha256"], after_file["sha256"])
            self.assertNotIn("node_modules/ignored.js", {item["path"] for item in after["files"]})


if __name__ == "__main__":
    unittest.main()
