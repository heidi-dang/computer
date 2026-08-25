import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path

from cptr.utils.git import diff


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


class GitDiffTests(unittest.TestCase):
    def test_untracked_diff_includes_non_ignored_files_with_bounded_content(self):
        with tempfile.TemporaryDirectory() as workspace:
            root = Path(workspace)
            _git(root, "init")
            _git(root, "config", "user.email", "test@example.invalid")
            _git(root, "config", "user.name", "Test User")
            (root / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
            (root / "tracked.txt").write_text("base\n", encoding="utf-8")
            _git(root, "add", ".gitignore", "tracked.txt")
            _git(root, "commit", "-m", "base")

            (root / "new.txt").write_text("visible\n" + ("x" * 3000) + "\n", encoding="utf-8")
            (root / "ignored.txt").write_text("must not appear\n", encoding="utf-8")

            result = asyncio.run(diff(str(root), untracked=True))

        paths = [item["path"] for item in result["files"]]
        self.assertEqual(paths, ["new.txt"])
        added_lines = [
            line["content"]
            for line in result["files"][0]["hunks"][0]["lines"]
            if line["type"] == "added"
        ]
        self.assertEqual(added_lines[0], "visible")
        self.assertLessEqual(max(len(line) for line in added_lines), 2000)
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
