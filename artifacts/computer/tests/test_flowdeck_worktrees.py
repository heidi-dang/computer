import subprocess
import tempfile
import unittest
from pathlib import Path

from cptr.flowdeck.worktrees import (
    commit_worktree,
    create_worktree,
    integrate_worktree,
    remove_worktree,
    validate_execution_worktree,
    worktree_changed_paths,
)


def _git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ("git", "-C", str(root), *args),
        text=True,
        stderr=subprocess.STDOUT,
    ).strip()


class BuildWorktreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-q")
        _git(self.root, "config", "user.name", "CPTR Test")
        _git(self.root, "config", "user.email", "cptr@example.invalid")
        (self.root / "README.md").write_text("base\n")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-qm", "base")

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_nodes_share_base_and_integrate_only_after_commit(self):
        base = _git(self.root, "rev-parse", "HEAD")
        left = await create_worktree(
            canonical_workspace=str(self.root),
            run_id="run-1",
            node_key="backend",
            common_base=base,
        )
        right = await create_worktree(
            canonical_workspace=str(self.root),
            run_id="run-1",
            node_key="frontend",
            common_base=base,
        )
        try:
            self.assertEqual(left.common_base, base)
            self.assertEqual(right.common_base, base)
            self.assertNotEqual(left.path, right.path)
            self.assertEqual(await validate_execution_worktree(str(self.root), left.path), Path(left.path))
            Path(left.path, "backend.py").write_text("print('ok')\n")
            self.assertEqual(await worktree_changed_paths(left), ("backend.py",))
            commit_hash = await commit_worktree(left, "CPTR build node backend")
            self.assertTrue(commit_hash)
            self.assertFalse((self.root / "backend.py").exists())
            result = await integrate_worktree(str(self.root), left, commit_hash)
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue((self.root / "backend.py").exists())
        finally:
            await remove_worktree(left)
            await remove_worktree(right)
        self.assertEqual(await validate_execution_worktree(str(self.root), str(self.root)), self.root)
        self.assertEqual(_git(self.root, "worktree", "list", "--porcelain").count("worktree "), 1)

    async def test_execution_path_must_belong_to_canonical_repository(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        with self.assertRaises(ValueError):
            await validate_execution_worktree(str(self.root), str(outside))
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            await validate_execution_worktree(str(self.root), str(alias / "missing"))

    async def test_conflicting_cherry_pick_is_reported_without_leaving_merge_state(self):
        base = _git(self.root, "rev-parse", "HEAD")
        left = await create_worktree(
            canonical_workspace=str(self.root),
            run_id="run-2",
            node_key="left",
            common_base=base,
        )
        right = await create_worktree(
            canonical_workspace=str(self.root),
            run_id="run-2",
            node_key="right",
            common_base=base,
        )
        try:
            Path(left.path, "README.md").write_text("left\n")
            left_commit = await commit_worktree(left, "left")
            Path(right.path, "README.md").write_text("right\n")
            right_commit = await commit_worktree(right, "right")
            self.assertEqual((await integrate_worktree(str(self.root), left, left_commit))["status"], "succeeded")
            result = await integrate_worktree(str(self.root), right, right_commit)
            self.assertEqual(result["status"], "conflict")
            self.assertFalse(Path(self.root, ".git", "CHERRY_PICK_HEAD").exists())
        finally:
            await remove_worktree(left)
            await remove_worktree(right)


if __name__ == "__main__":
    unittest.main()

