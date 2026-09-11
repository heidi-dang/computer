"""Tests for revision divergence, branch tracking, and WorkspaceTask aggregation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import (
    Base,
    DirectCodingWorker,
    User,
    Workspace,
)
from cptr.services.workspace_tasks import (
    WorkspaceTaskError,
    WorkspaceTaskService,
)
from cptr.utils.git import current_branch, revision_divergence


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> str:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "CPTR Tests")
    (root / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial commit")
    return _git(root, "rev-parse", "HEAD")


class GitRevisionDivergenceMechanicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_revision_divergence_when_revisions_are_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base_rev = _init_repo(root)

            div = await revision_divergence(str(root), base_rev, "HEAD")
            self.assertEqual(div, {"behind": 0, "ahead": 0})

    async def test_revision_divergence_when_target_is_ahead(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base_rev = _init_repo(root)

            (root / "file1.txt").write_text("1\n", encoding="utf-8")
            _git(root, "add", "file1.txt")
            _git(root, "commit", "-qm", "commit 1")

            (root / "file2.txt").write_text("2\n", encoding="utf-8")
            _git(root, "add", "file2.txt")
            _git(root, "commit", "-qm", "commit 2")

            div = await revision_divergence(str(root), base_rev, "HEAD")
            self.assertEqual(div, {"behind": 0, "ahead": 2})

    async def test_revision_divergence_when_target_is_behind(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base_rev = _init_repo(root)

            (root / "file1.txt").write_text("1\n", encoding="utf-8")
            _git(root, "add", "file1.txt")
            _git(root, "commit", "-qm", "commit 1")
            new_head = _git(root, "rev-parse", "HEAD")

            # Check from new_head (as base) to base_rev (as target): target is behind by 1 commit
            div = await revision_divergence(str(root), new_head, base_rev)
            self.assertEqual(div, {"behind": 1, "ahead": 0})

    async def test_revision_divergence_when_branches_diverge(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _init_repo(root)

            # Create feature branch with 2 commits
            _git(root, "checkout", "-qb", "feature")
            (root / "f1.txt").write_text("feat1\n", encoding="utf-8")
            _git(root, "add", "f1.txt")
            _git(root, "commit", "-qm", "feat 1")
            (root / "f2.txt").write_text("feat2\n", encoding="utf-8")
            _git(root, "add", "f2.txt")
            _git(root, "commit", "-qm", "feat 2")
            feature_rev = _git(root, "rev-parse", "HEAD")

            # Go back to default branch and add 1 commit
            _git(root, "checkout", "-q", "-")
            (root / "main1.txt").write_text("main1\n", encoding="utf-8")
            _git(root, "add", "main1.txt")
            _git(root, "commit", "-qm", "main 1")
            main_rev = _git(root, "rev-parse", "HEAD")

            # feature vs main: feature has 2 commits not in main, main has 1 commit not in feature
            # base = main_rev, target = feature_rev
            div = await revision_divergence(str(root), main_rev, feature_rev)
            self.assertEqual(div, {"behind": 1, "ahead": 2})

    async def test_revision_divergence_invalid_ref_or_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            div = await revision_divergence(str(root), "non-existent", "HEAD")
            self.assertEqual(div, {"behind": 0, "ahead": 0})

    async def test_current_branch_and_detached_head(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _init_repo(root)

            branch = await current_branch(str(root))
            self.assertTrue(len(branch) > 0)
            self.assertNotEqual(branch, "HEAD")

            # Detach HEAD
            _git(root, "checkout", "--detach", "HEAD")
            detached = await current_branch(str(root))
            self.assertEqual(detached, "")


class WorkspaceTaskAggregationServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.service = WorkspaceTaskService()

        # Seed User and Workspace
        async with self.sessions() as db:
            user = User(
                id="usr_test_1",
                display_name="Test User",
                role="user",
                created_at=1,
            )
            workspace = Workspace(
                id="ws_test_1",
                user_id="usr_test_1",
                path="/tmp/test_workspace",
                name="test_ws",
                data={},
                created_at=1,
                updated_at=1,
            )
            db.add(user)
            db.add(workspace)
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_task_creation_and_status_transitions(self):
        with patch(
            "cptr.services.workspace_tasks.get_db", new=AsyncMock(side_effect=self.sessions)
        ):
            # Create task
            task = await self.service.create_task(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                title="Refactor auth module",
                description="Consolidate tokens",
                metadata={"priority": "high"},
            )
            self.assertTrue(task["id"].startswith("wst_"))
            self.assertEqual(task["status"], "OPEN")
            self.assertEqual(task["title"], "Refactor auth module")
            self.assertEqual(task["metadata"], {"priority": "high"})
            self.assertIsNone(task["closed_at"])

            # Transition to IN_PROGRESS
            updated = await self.service.update_task_status(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
                status="IN_PROGRESS",
            )
            self.assertEqual(updated["status"], "IN_PROGRESS")
            self.assertIsNone(updated["closed_at"])

            # Transition to VERIFIED
            verified = await self.service.update_task_status(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
                status="VERIFIED",
            )
            self.assertEqual(verified["status"], "VERIFIED")
            self.assertIsNone(verified["closed_at"])

            # Transition to CLOSED
            closed = await self.service.update_task_status(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
                status="CLOSED",
            )
            self.assertEqual(closed["status"], "CLOSED")
            self.assertIsNotNone(closed["closed_at"])

            # Invalid status raises error
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.update_task_status(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    task_id=task["id"],
                    status="UNKNOWN_STATUS",
                )
            self.assertEqual(caught.exception.code, "WORKSPACE_TASK_INVALID_STATUS")

    async def test_repository_pinning_and_divergence_tracking(self):
        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            repo_dir = ws_root / "frontend"
            repo_dir.mkdir()
            base_rev = _init_repo(repo_dir)

            with patch(
                "cptr.services.workspace_tasks.get_db", new=AsyncMock(side_effect=self.sessions)
            ):
                task = await self.service.create_task(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    title="Frontend multi-repo task",
                )

                # Pin repository (workspace must carry id for ownership verification)
                pin = await self.service.pin_repository(
                    user_id="usr_test_1",
                    workspace={"id": "ws_test_1", "path": str(ws_root)},
                    task_id=task["id"],
                    repo_path="frontend",
                )
                self.assertEqual(pin["pinned_revision"], base_rev)
                self.assertEqual(pin["repo_path"], "frontend")

                # Initial divergence check: clean, not diverged
                divergences = await self.service.check_divergence(
                    user_id="usr_test_1",
                    workspace={"id": "ws_test_1", "path": str(ws_root)},
                    task_id=task["id"],
                )
                self.assertEqual(len(divergences), 1)
                self.assertFalse(divergences[0]["is_diverged"])
                self.assertEqual(divergences[0]["ahead"], 0)
                self.assertEqual(divergences[0]["behind"], 0)

                # Commit 2 changes in repo
                (repo_dir / "index.js").write_text("console.log(1);\n", encoding="utf-8")
                _git(repo_dir, "add", "index.js")
                _git(repo_dir, "commit", "-qm", "feat: add index.js")

                (repo_dir / "index.js").write_text("console.log(2);\n", encoding="utf-8")
                _git(repo_dir, "add", "index.js")
                _git(repo_dir, "commit", "-qm", "feat: update index.js")

                # Divergence check: ahead by 2
                divergences_after = await self.service.check_divergence(
                    user_id="usr_test_1",
                    workspace={"id": "ws_test_1", "path": str(ws_root)},
                    task_id=task["id"],
                )
                self.assertEqual(len(divergences_after), 1)
                self.assertTrue(divergences_after[0]["is_diverged"])
                self.assertEqual(divergences_after[0]["ahead"], 2)
                self.assertEqual(divergences_after[0]["behind"], 0)

    async def test_worker_link_aggregation_and_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            base_rev = _init_repo(ws_root)
            worker_root = ws_root / "worker_worktree"
            worker_root.mkdir()
            _init_repo(worker_root)

            with patch(
                "cptr.services.workspace_tasks.get_db", new=AsyncMock(side_effect=self.sessions)
            ):
                # Seed DirectCodingWorker
                async with self.sessions() as db:
                    worker = DirectCodingWorker(
                        id="dcw_test_worker_1",
                        user_id="usr_test_1",
                        workspace_id="ws_test_1",
                        name="Frontend worker",
                        responsibility="components",
                        repo_path=".",
                        status="READY",
                        branch="cptr/direct/dcw_test_worker_1",
                        worktree_path=str(worker_root),
                        base_revision=base_rev,
                        created_at=10,
                        updated_at=10,
                    )
                    db.add(worker)
                    await db.commit()

                task = await self.service.create_task(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    title="Task with worker",
                )

                # Link worker to task
                link = await self.service.link_worker(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    task_id=task["id"],
                    worker_id="dcw_test_worker_1",
                    role="lead",
                )
                self.assertEqual(link["role"], "lead")
                self.assertEqual(link["status"], "ACTIVE")

                # List workers
                workers = await self.service.list_workers(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    task_id=task["id"],
                )
                self.assertEqual(len(workers), 1)
                self.assertEqual(workers[0]["worker_id"], "dcw_test_worker_1")
                self.assertEqual(workers[0]["worker"]["name"], "Frontend worker")

                # Unlink worker
                await self.service.unlink_worker(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    task_id=task["id"],
                    worker_id="dcw_test_worker_1",
                    status="DETACHED",
                )
                workers_after = await self.service.list_workers(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    task_id=task["id"],
                )
                self.assertEqual(workers_after[0]["status"], "DETACHED")

    async def test_verification_evidence_aggregation(self):
        with patch(
            "cptr.services.workspace_tasks.get_db", new=AsyncMock(side_effect=self.sessions)
        ):
            task = await self.service.create_task(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                title="Verification test task",
            )

            # Add PASSED test evidence
            ev1 = await self.service.add_evidence(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
                kind="test",
                status="PASSED",
                summary="Unit tests passed (5 passed)",
                command="pytest tests/unit",
                details={"passed": 5, "failed": 0},
            )
            self.assertEqual(ev1["status"], "PASSED")

            # Add PASSED typecheck evidence
            ev2 = await self.service.add_evidence(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
                kind="typecheck",
                status="PASSED",
                summary="mypy passed with 0 errors",
                command="mypy cptr",
            )
            self.assertEqual(ev2["status"], "PASSED")

            # Check verification summary: should be verified
            summary1 = await self.service.verification_summary(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
            )
            self.assertEqual(summary1["total"], 2)
            self.assertEqual(summary1["passed"], 2)
            self.assertEqual(summary1["failed"], 0)
            self.assertTrue(summary1["verified"])

            # Add a FAILED lint evidence
            await self.service.add_evidence(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
                kind="lint",
                status="FAILED",
                summary="ruff found 2 errors",
                command="ruff check",
            )

            summary2 = await self.service.verification_summary(
                user_id="usr_test_1",
                workspace_id="ws_test_1",
                task_id=task["id"],
            )
            self.assertEqual(summary2["total"], 3)
            self.assertEqual(summary2["failed"], 1)
            self.assertFalse(summary2["verified"])
            self.assertIn("[lint] ruff found 2 errors", summary2["failures"])

    async def test_full_workspace_task_summary_aggregation(self):
        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            _init_repo(ws_root)
            with patch(
                "cptr.services.workspace_tasks.get_db", new=AsyncMock(side_effect=self.sessions)
            ):
                task = await self.service.create_task(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    title="Full aggregation test",
                )
                await self.service.pin_repository(
                    user_id="usr_test_1",
                    workspace={"id": "ws_test_1", "path": str(ws_root)},
                    task_id=task["id"],
                    repo_path=".",
                )
                await self.service.add_evidence(
                    user_id="usr_test_1",
                    workspace_id="ws_test_1",
                    task_id=task["id"],
                    kind="test",
                    status="PASSED",
                    summary="Full suite passed",
                )

                summary = await self.service.summary(
                    user_id="usr_test_1",
                    workspace={"id": "ws_test_1", "path": str(ws_root)},
                    task_id=task["id"],
                )
                self.assertEqual(summary["task"]["id"], task["id"])
                self.assertEqual(len(summary["repositories"]), 1)
                self.assertFalse(summary["has_divergence"])
                self.assertTrue(summary["all_passed"])


if __name__ == "__main__":
    unittest.main()
