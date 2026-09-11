"""Security/state-machine hardening tests for WorkspaceTask service.

Covers:
  1. pin_repository ownership hole: empty workspace_id must fail closed.
  2. head_revision capture: pin_repository captures and updates head_revision.
  3. Per-worker list failure containment: one corrupt worker cannot abort aggregation.
  4. OPEN->INTEGRATED gate: transition blocked unless current status is VERIFIED.
"""

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


class WorkspaceTaskHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.service = WorkspaceTaskService()

        async with self.sessions() as db:
            user = User(
                id="usr_harden_1",
                display_name="Hardening User",
                role="user",
                created_at=1,
            )
            workspace = Workspace(
                id="ws_harden_1",
                user_id="usr_harden_1",
                path="/tmp/harden_ws",
                name="harden_ws",
                data={},
                created_at=1,
                updated_at=1,
            )
            db.add(user)
            db.add(workspace)
            await db.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------------
    # 1. pin_repository: empty workspace_id must fail closed (no bypass)
    # ------------------------------------------------------------------

    async def test_pin_repository_fails_closed_when_workspace_id_missing(self) -> None:
        """Passing a raw path string (no .id attribute) must be rejected immediately."""
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.pin_repository(
                    user_id="usr_harden_1",
                    # Plain string — no workspace_id extractable
                    workspace="/tmp/some_path",
                    task_id="wst_doesnotexist",
                    repo_path=".",
                )
            self.assertEqual(caught.exception.code, "WORKSPACE_TASK_MISSING_WORKSPACE_ID")
            self.assertEqual(caught.exception.status_code, 422)

    async def test_pin_repository_fails_closed_when_dict_lacks_id(self) -> None:
        """dict workspace without 'id' key must be rejected."""
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.pin_repository(
                    user_id="usr_harden_1",
                    workspace={"path": "/tmp/some_path"},  # no "id"
                    task_id="wst_doesnotexist",
                    repo_path=".",
                )
            self.assertEqual(caught.exception.code, "WORKSPACE_TASK_MISSING_WORKSPACE_ID")

    async def test_pin_repository_ownership_enforced_cross_user(self) -> None:
        """A valid workspace_id belonging to a different user must not allow pinning."""
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            # Create task for usr_harden_1
            task = await self.service.create_task(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                title="Cross-user ownership test",
            )
            # Attempt to pin as a different user with wrong workspace_id
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.pin_repository(
                    user_id="usr_harden_1",
                    workspace={"id": "ws_WRONG_WORKSPACE", "path": "/tmp/x"},
                    task_id=task["id"],
                    repo_path=".",
                )
            # Should fail because task not found under wrong workspace_id
            self.assertIn(
                caught.exception.code,
                {
                    "WORKSPACE_TASK_NOT_FOUND",
                    "WORKSPACE_TASK_NOT_GIT_REPO",
                },
            )

    # ------------------------------------------------------------------
    # 2. head_revision: captured and updated on pin_repository
    # ------------------------------------------------------------------

    async def test_pin_repository_captures_head_revision_on_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            repo_dir = ws_root / "service"
            repo_dir.mkdir()
            base_rev = _init_repo(repo_dir)

            with patch(
                "cptr.services.workspace_tasks.get_db",
                new=AsyncMock(side_effect=self.sessions),
            ):
                task = await self.service.create_task(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    title="head_revision capture test",
                )
                pin = await self.service.pin_repository(
                    user_id="usr_harden_1",
                    workspace={"id": "ws_harden_1", "path": str(ws_root)},
                    task_id=task["id"],
                    repo_path="service",
                )
                self.assertEqual(pin["pinned_revision"], base_rev)
                self.assertEqual(pin["head_revision"], base_rev)

    async def test_pin_repository_updates_head_revision_on_subsequent_commits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            repo_dir = ws_root / "svc2"
            repo_dir.mkdir()
            base_rev = _init_repo(repo_dir)

            with patch(
                "cptr.services.workspace_tasks.get_db",
                new=AsyncMock(side_effect=self.sessions),
            ):
                task = await self.service.create_task(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    title="head_revision update test",
                )
                # Pin at base_rev
                pin1 = await self.service.pin_repository(
                    user_id="usr_harden_1",
                    workspace={"id": "ws_harden_1", "path": str(ws_root)},
                    task_id=task["id"],
                    repo_path="svc2",
                )
                self.assertEqual(pin1["head_revision"], base_rev)

                # Advance the repo HEAD by one commit
                (repo_dir / "new.txt").write_text("change\n", encoding="utf-8")
                _git(repo_dir, "add", "new.txt")
                _git(repo_dir, "commit", "-qm", "advance")
                new_rev = _git(repo_dir, "rev-parse", "HEAD")

                # Re-pin: pinned_revision stays base_rev (explicitly supplied),
                # but head_revision must advance to new_rev
                pin2 = await self.service.pin_repository(
                    user_id="usr_harden_1",
                    workspace={"id": "ws_harden_1", "path": str(ws_root)},
                    task_id=task["id"],
                    repo_path="svc2",
                    pinned_revision=base_rev,
                )
                self.assertEqual(pin2["pinned_revision"], base_rev)
                self.assertEqual(pin2["head_revision"], new_rev)
                self.assertNotEqual(pin2["pinned_revision"], pin2["head_revision"])

    # ------------------------------------------------------------------
    # 3. Per-worker error containment: list_workers must not abort
    # ------------------------------------------------------------------

    async def test_list_workers_contains_per_worker_failure(self) -> None:
        """A worker that raises during summary must produce an error entry, not abort."""
        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            good_wt = ws_root / "good_worktree"
            good_wt.mkdir()
            base_rev = _init_repo(good_wt)

            with patch(
                "cptr.services.workspace_tasks.get_db",
                new=AsyncMock(side_effect=self.sessions),
            ):
                async with self.sessions() as db:
                    # Good worker
                    good_worker = DirectCodingWorker(
                        id="dcw_good_h1",
                        user_id="usr_harden_1",
                        workspace_id="ws_harden_1",
                        name="Good Worker",
                        responsibility="backend",
                        repo_path=".",
                        status="READY",
                        branch="cptr/direct/dcw_good_h1",
                        worktree_path=str(good_wt),
                        base_revision=base_rev,
                        created_at=10,
                        updated_at=10,
                    )
                    # Bad worker (worktree_path points to non-existent directory)
                    bad_worker = DirectCodingWorker(
                        id="dcw_bad_h1",
                        user_id="usr_harden_1",
                        workspace_id="ws_harden_1",
                        name="Bad Worker",
                        responsibility="frontend",
                        repo_path=".",
                        status="READY",
                        branch="cptr/direct/dcw_bad_h1",
                        worktree_path="/nonexistent/path/that/does/not/exist",
                        base_revision="deadbeef" * 5,  # invalid sha
                        created_at=10,
                        updated_at=10,
                    )
                    db.add(good_worker)
                    db.add(bad_worker)
                    await db.commit()

                task = await self.service.create_task(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    title="Per-worker containment test",
                )

                await self.service.link_worker(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    task_id=task["id"],
                    worker_id="dcw_good_h1",
                )
                await self.service.link_worker(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    task_id=task["id"],
                    worker_id="dcw_bad_h1",
                )

                # Patch direct_worker_service.summary to raise for the bad worker
                async def _mock_summary(worker: DirectCodingWorker) -> dict:
                    if worker.id == "dcw_bad_h1":
                        raise RuntimeError("simulated corrupt worker summary")
                    return {"name": worker.name, "status": worker.status, "changed_file_count": 0}

                with patch(
                    "cptr.services.workspace_tasks.direct_worker_service.summary",
                    side_effect=_mock_summary,
                ):
                    workers = await self.service.list_workers(
                        user_id="usr_harden_1",
                        workspace_id="ws_harden_1",
                        task_id=task["id"],
                    )

                # Must get BOTH workers back — aggregation not aborted
                self.assertEqual(len(workers), 2)
                worker_ids = {w["worker_id"] for w in workers}
                self.assertIn("dcw_good_h1", worker_ids)
                self.assertIn("dcw_bad_h1", worker_ids)

                # The bad worker entry must have an error key
                bad_entry = next(w for w in workers if w["worker_id"] == "dcw_bad_h1")
                self.assertIn("error", bad_entry)
                self.assertIn("simulated corrupt worker", bad_entry["error"])
                self.assertEqual(bad_entry["worker"], {})

                # The good worker entry must NOT have an error key
                good_entry = next(w for w in workers if w["worker_id"] == "dcw_good_h1")
                self.assertNotIn("error", good_entry)

    # ------------------------------------------------------------------
    # 4. INTEGRATED state-machine gate
    # ------------------------------------------------------------------

    async def test_integrated_requires_verified_state(self) -> None:
        """OPEN -> INTEGRATED must be blocked; only VERIFIED -> INTEGRATED is allowed."""
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            task = await self.service.create_task(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                title="State machine gate test",
            )
            self.assertEqual(task["status"], "OPEN")

            # OPEN -> INTEGRATED must fail
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.update_task_status(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    task_id=task["id"],
                    status="INTEGRATED",
                )
            self.assertEqual(caught.exception.code, "WORKSPACE_TASK_INVALID_TRANSITION")
            self.assertEqual(caught.exception.status_code, 422)

    async def test_in_progress_to_integrated_blocked(self) -> None:
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            task = await self.service.create_task(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                title="IN_PROGRESS gate test",
            )
            await self.service.update_task_status(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                status="IN_PROGRESS",
            )
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.update_task_status(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    task_id=task["id"],
                    status="INTEGRATED",
                )
            self.assertEqual(caught.exception.code, "WORKSPACE_TASK_INVALID_TRANSITION")

    async def test_verified_to_integrated_allowed(self) -> None:
        """VERIFIED -> INTEGRATED is the only permitted path into INTEGRATED."""
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            task = await self.service.create_task(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                title="VERIFIED->INTEGRATED gate test",
            )
            # Drive to VERIFIED
            await self.service.update_task_status(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                status="IN_PROGRESS",
            )
            await self.service.update_task_status(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                status="VERIFIED",
            )
            await self.service.add_evidence(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                kind="test",
                status="PASSED",
                summary="verification suite passed",
            )
            # VERIFIED -> INTEGRATED succeeds only with passing evidence
            integrated = await self.service.update_task_status(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                status="INTEGRATED",
            )
            self.assertEqual(integrated["status"], "INTEGRATED")
            self.assertIsNotNone(integrated["closed_at"])

    async def test_verified_without_evidence_cannot_transition_to_integrated(self) -> None:
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            task = await self.service.create_task(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                title="VERIFIED without evidence",
            )
            await self.service.update_task_status(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                status="VERIFIED",
            )
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.update_task_status(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    task_id=task["id"],
                    status="INTEGRATED",
                )
            self.assertEqual(
                caught.exception.code,
                "WORKSPACE_TASK_VERIFICATION_REQUIRED",
            )

    async def test_failed_evidence_blocks_integrated_transition(self) -> None:
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            task = await self.service.create_task(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                title="VERIFIED with failed evidence",
            )
            await self.service.update_task_status(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                status="VERIFIED",
            )
            await self.service.add_evidence(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                kind="test",
                status="PASSED",
                summary="unit tests passed",
            )
            await self.service.add_evidence(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                kind="lint",
                status="FAILED",
                summary="lint failed",
            )
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.update_task_status(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    task_id=task["id"],
                    status="INTEGRATED",
                )
            self.assertEqual(
                caught.exception.code,
                "WORKSPACE_TASK_VERIFICATION_REQUIRED",
            )

    async def test_closed_task_cannot_transition_to_integrated(self) -> None:
        """CLOSED -> INTEGRATED must be blocked."""
        with patch(
            "cptr.services.workspace_tasks.get_db",
            new=AsyncMock(side_effect=self.sessions),
        ):
            task = await self.service.create_task(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                title="CLOSED gate test",
            )
            await self.service.update_task_status(
                user_id="usr_harden_1",
                workspace_id="ws_harden_1",
                task_id=task["id"],
                status="CLOSED",
            )
            with self.assertRaises(WorkspaceTaskError) as caught:
                await self.service.update_task_status(
                    user_id="usr_harden_1",
                    workspace_id="ws_harden_1",
                    task_id=task["id"],
                    status="INTEGRATED",
                )
            self.assertEqual(caught.exception.code, "WORKSPACE_TASK_INVALID_TRANSITION")


if __name__ == "__main__":
    unittest.main()
