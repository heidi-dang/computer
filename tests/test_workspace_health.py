import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import cptr.utils.db as db_module
from cptr.models import Base, DirectCodingWorker, User, Workspace
from cptr.services.workspace_health import (
    HealthBand,
    WorkerClassification,
    classify_worker,
    classify_workspace_health,
    classify_worktree_noise,
    conservative_reconcile_workspace,
    is_worktree_noise_path,
)
from cptr.utils.db import get_db
from cptr.utils.tools import command_sessions


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "CPTR Tests")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial commit")


class _HealthDbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._health_db_temp = tempfile.TemporaryDirectory()
        self._previous_engine = db_module._engine
        self._previous_session = db_module._async_session
        db_path = Path(self._health_db_temp.name) / "workspace-health.db"
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        db_module._engine = engine
        db_module._async_session = async_sessionmaker(engine, expire_on_commit=False)
        self._health_test_engine = engine

    async def asyncTearDown(self):
        await self._health_test_engine.dispose()
        db_module._engine = self._previous_engine
        db_module._async_session = self._previous_session
        self._health_db_temp.cleanup()


class WorktreeNoiseClassificationTests(unittest.TestCase):
    def test_noise_path_detection(self):
        self.assertTrue(is_worktree_noise_path(".cptr-worktrees/computer/dcw_123"))
        self.assertTrue(is_worktree_noise_path(".cptr-worktrees"))
        self.assertTrue(is_worktree_noise_path(".cptr/worktrees/w1"))
        self.assertTrue(is_worktree_noise_path(".cptr/cache"))
        self.assertFalse(is_worktree_noise_path("src/app.py"))
        self.assertFalse(is_worktree_noise_path("package.json"))

    def test_noise_path_with_known_paths(self):
        known = {"/abs/path/to/custom_worker", "rel/custom_worker"}
        self.assertTrue(is_worktree_noise_path("rel/custom_worker", known_worktree_paths=known))
        self.assertTrue(
            is_worktree_noise_path("rel/custom_worker/file.txt", known_worktree_paths=known)
        )
        self.assertFalse(is_worktree_noise_path("rel/other_file.txt", known_worktree_paths=known))

    def test_classify_worktree_noise_separates_noise_from_real_changes(self):
        files = [
            {"path": ".cptr-worktrees/worker1", "status": "untracked"},
            {"path": ".cptr-worktrees/worker2/file.py", "status": "untracked"},
            {"path": "src/index.ts", "status": "modified"},
            {"path": "docs/readme.md", "status": "added"},
        ]
        result = classify_worktree_noise(files)
        self.assertTrue(result["has_noise"])
        self.assertEqual(
            result["noise_paths"], [".cptr-worktrees/worker1", ".cptr-worktrees/worker2/file.py"]
        )
        self.assertEqual(result["real_changes"], ["docs/readme.md", "src/index.ts"])
        self.assertFalse(result["is_clean_strict"])
        self.assertFalse(result["is_clean_excluding_noise"])

    def test_classify_worktree_noise_clean_excluding_noise(self):
        files = [
            {"path": ".cptr-worktrees/worker1", "status": "untracked"},
        ]
        result = classify_worktree_noise(files)
        self.assertTrue(result["has_noise"])
        self.assertEqual(result["noise_paths"], [".cptr-worktrees/worker1"])
        self.assertEqual(result["real_changes"], [])
        self.assertFalse(result["is_clean_strict"])
        self.assertTrue(result["is_clean_excluding_noise"])


class StaleWorkerMultiSignalClassificationTests(_HealthDbCase):
    def setUp(self):
        command_sessions.clear()

    def tearDown(self):
        command_sessions.clear()

    async def test_worker_closed_classification(self):
        worker = DirectCodingWorker(
            id="dcw_closed",
            user_id="u1",
            workspace_id="ws1",
            name="closed worker",
            status="CLOSED",
            branch="cptr/direct/dcw_closed",
            worktree_path="/tmp/fake_dir",
            base_revision="rev1",
            created_at=1000,
            updated_at=1000,
            closed_at=2000,
        )
        report = await classify_worker(worker)
        self.assertEqual(report["classification"], WorkerClassification.CLOSED.value)

    async def test_worker_missing_worktree_classification(self):
        worker = DirectCodingWorker(
            id="dcw_missing",
            user_id="u1",
            workspace_id="ws1",
            name="missing worker",
            status="READY",
            branch="cptr/direct/dcw_missing",
            worktree_path="/tmp/nonexistent_dir_999999",
            base_revision="rev1",
            created_at=1000,
            updated_at=1000,
        )
        report = await classify_worker(worker)
        self.assertEqual(report["classification"], WorkerClassification.MISSING_WORKTREE.value)
        self.assertFalse(report["signals"]["worktree_exists"])

    async def test_worker_active_overlap_with_running_command(self):
        with tempfile.TemporaryDirectory() as temp:
            wt_dir = Path(temp) / "worktree"
            wt_dir.mkdir()
            _init_repo(wt_dir)

            command_sessions["cmd_1"] = {
                "id": "cmd_1",
                "workspace": str(wt_dir),
                "done": False,
                "created_at": time.time(),
            }

            worker = DirectCodingWorker(
                id="dcw_active",
                user_id="u1",
                workspace_id="ws1",
                name="active worker",
                status="WORKING",
                branch="cptr/direct/dcw_active",
                worktree_path=str(wt_dir),
                base_revision="rev1",
                created_at=1000,
                updated_at=1000,
            )
            report = await classify_worker(worker)
            self.assertEqual(report["classification"], WorkerClassification.ACTIVE_OVERLAP.value)
            self.assertEqual(report["signals"]["active_command_count"], 1)

    async def test_worker_stale_noop_classification(self):
        with tempfile.TemporaryDirectory() as temp:
            wt_dir = Path(temp) / "worktree"
            wt_dir.mkdir()
            _init_repo(wt_dir)

            now_ms = int(time.time() * 1000)
            worker = DirectCodingWorker(
                id="dcw_stale_noop",
                user_id="u1",
                workspace_id="ws1",
                name="stale noop",
                status="WORKING",
                branch="cptr/direct/dcw_stale_noop",
                worktree_path=str(wt_dir),
                base_revision="rev1",
                created_at=now_ms - 600_000,
                updated_at=now_ms - 600_000,
                last_activity_at=now_ms - 600_000,
            )
            report = await classify_worker(worker, now_ms=now_ms, stale_threshold_seconds=300.0)
            self.assertEqual(report["classification"], WorkerClassification.STALE_NOOP.value)
            self.assertEqual(report["signals"]["changed_files_count"], 0)

    async def test_worker_stale_with_changes_classification(self):
        with tempfile.TemporaryDirectory() as temp:
            wt_dir = Path(temp) / "worktree"
            wt_dir.mkdir()
            _init_repo(wt_dir)
            (wt_dir / "app.py").write_text("value = 999\n", encoding="utf-8")

            now_ms = int(time.time() * 1000)
            worker = DirectCodingWorker(
                id="dcw_stale_dirty",
                user_id="u1",
                workspace_id="ws1",
                name="stale dirty",
                status="WORKING",
                branch="cptr/direct/dcw_stale_dirty",
                worktree_path=str(wt_dir),
                base_revision="rev1",
                created_at=now_ms - 600_000,
                updated_at=now_ms - 600_000,
                last_activity_at=now_ms - 600_000,
            )
            report = await classify_worker(worker, now_ms=now_ms, stale_threshold_seconds=300.0)
            self.assertEqual(
                report["classification"], WorkerClassification.STALE_WITH_CHANGES.value
            )
            self.assertGreater(report["signals"]["changed_files_count"], 0)
            self.assertIn("app.py", report["signals"]["changed_paths"])

    async def test_worker_already_integrated_unclosed(self):
        with tempfile.TemporaryDirectory() as temp:
            wt_dir = Path(temp) / "worktree"
            wt_dir.mkdir()
            _init_repo(wt_dir)

            now_ms = int(time.time() * 1000)
            worker = DirectCodingWorker(
                id="dcw_integrated",
                user_id="u1",
                workspace_id="ws1",
                name="integrated worker",
                status="INTEGRATED",
                branch="cptr/direct/dcw_integrated",
                worktree_path=str(wt_dir),
                base_revision="rev1",
                created_at=now_ms - 100_000,
                updated_at=now_ms - 50_000,
                integrated_at=now_ms - 50_000,
            )
            report = await classify_worker(worker, now_ms=now_ms)
            self.assertEqual(
                report["classification"], WorkerClassification.ALREADY_INTEGRATED_UNCLOSED.value
            )
            self.assertTrue(report["signals"]["is_integrated"])


class ConservativeReconcileAndHealthIntegrationTests(_HealthDbCase):
    def setUp(self):
        command_sessions.clear()

    def tearDown(self):
        command_sessions.clear()

    async def test_classify_workspace_health_unavailable(self):
        workspace = Workspace(
            id="ws_missing",
            user_id="u_test",
            path="/tmp/nonexistent_workspace_path_12345",
            name="Missing Workspace",
            data={},
            created_at=1000,
        )
        report = await classify_workspace_health(workspace=workspace, user_id="u_test")
        self.assertFalse(report["available"])
        self.assertEqual(report["health_band"], HealthBand.UNHEALTHY.value)

    async def test_conservative_reconcile_preserves_worktrees_and_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            base_repo = Path(temp) / "repo"
            base_repo.mkdir()
            _init_repo(base_repo)

            wt_dir = Path(temp) / "worktrees" / "worker1"
            wt_dir.mkdir(parents=True)
            _init_repo(wt_dir)
            # Write a change in the worker's worktree
            (wt_dir / "app.py").write_text("preserved_change = True\n", encoding="utf-8")

            now_ms = int(time.time() * 1000)

            # Ensure user exists for foreign key constraint
            user_id = f"user_rec_{now_ms}"
            workspace_id = f"ws_{now_ms}"
            worker_id = f"dcw_{now_ms}"

            async with await get_db() as db:
                user = User(id=user_id, role="user", settings={}, created_at=now_ms)
                workspace = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(base_repo),
                    name="Test Workspace",
                    data={},
                    created_at=now_ms - 1000_000,
                )
                worker = DirectCodingWorker(
                    id=worker_id,
                    user_id=user_id,
                    workspace_id=workspace.id,
                    name="Stale Worker With Changes",
                    status="WORKING",
                    branch="cptr/direct/test_branch",
                    worktree_path=str(wt_dir),
                    base_revision=_git(base_repo, "rev-parse", "HEAD"),
                    created_at=now_ms - 600_000,
                    updated_at=now_ms - 600_000,
                    last_activity_at=now_ms - 600_000,
                )
                db.add(user)
                await db.flush()  # satisfy users FK before workspace insert
                db.add(workspace)
                await db.flush()  # satisfy workspaces FK before worker insert
                db.add(worker)
                await db.commit()

            # Pre-reconcile health check
            health_before = await classify_workspace_health(
                workspace=workspace,
                user_id=user_id,
            )
            self.assertTrue(health_before["available"])
            self.assertEqual(health_before["summary"]["stale_with_changes"], 1)

            # Run conservative reconcile
            reconcile_result = await conservative_reconcile_workspace(
                workspace=workspace,
                user_id=user_id,
            )

            # Safety guarantees verification:
            # 1. Never delete or discard
            self.assertFalse(reconcile_result["discarded"])
            self.assertEqual(reconcile_result["deleted_worktrees_count"], 0)
            self.assertEqual(reconcile_result["deleted_branches_count"], 0)

            # 2. Worker file changes must remain intact on disk
            self.assertTrue(wt_dir.exists(), "Worktree directory must NOT be deleted")
            self.assertEqual(
                (wt_dir / "app.py").read_text(encoding="utf-8"),
                "preserved_change = True\n",
                "Uncommitted worker changes must NOT be discarded",
            )

            # 3. Worker record in DB was refreshed from WORKING to READY
            async with await get_db() as db:
                managed = await db.get(DirectCodingWorker, worker.id)
                self.assertIsNotNone(managed, "DirectCodingWorker row must NOT be deleted")
                self.assertEqual(managed.status, "READY")

            # 4. Refreshed workers list reflects the status transition
            refreshed = reconcile_result["refreshed_workers"]
            self.assertEqual(len(refreshed), 1)
            self.assertEqual(refreshed[0]["worker_id"], worker.id)
            self.assertEqual(refreshed[0]["previous_status"], "WORKING")
            self.assertEqual(refreshed[0]["new_status"], "READY")
            self.assertEqual(
                refreshed[0]["classification"], WorkerClassification.STALE_WITH_CHANGES.value
            )


class NewDetectionsTests(_HealthDbCase):
    """Tests for the new structural detections added to classify_workspace_health."""

    def setUp(self):
        command_sessions.clear()

    def tearDown(self):
        command_sessions.clear()

    # ---- Detached HEAD detection ----

    def test_is_detached_head_true(self):
        from cptr.services.workspace_health import _is_detached_head

        self.assertTrue(_is_detached_head("(detached)"))

    def test_is_detached_head_false_on_named_branch(self):
        from cptr.services.workspace_health import _is_detached_head

        self.assertFalse(_is_detached_head("main"))
        self.assertFalse(_is_detached_head("cptr/direct/dcw_abc123"))
        self.assertFalse(_is_detached_head(""))

    async def test_classify_workspace_health_detects_detached_head(self):
        """classify_workspace_health sets is_detached_head when git status branch == (detached)."""
        from unittest.mock import AsyncMock, patch as mock_patch
        from cptr.services.workspace_health import classify_workspace_health, HealthBand

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _init_repo(repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_detach_{now_ms}"
            workspace_id = f"ws_detach_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=user_id,
                        path=str(repo),
                        name="Detach Test",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            fake_status = {
                "branch": "(detached)",
                "upstream": "",
                "ahead": 0,
                "behind": 0,
                "files": [],
                "remote_url": "",
            }
            fake_wts = {"worktrees": []}

            with (
                mock_patch(
                    "cptr.services.workspace_health.git_status",
                    new=AsyncMock(return_value=fake_status),
                ),
                mock_patch(
                    "cptr.services.workspace_health.git_worktrees",
                    new=AsyncMock(return_value=fake_wts),
                ),
            ):
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(repo),
                    name="Detach Test",
                    data={},
                    created_at=now_ms,
                )
                report = await classify_workspace_health(workspace=ws, user_id=user_id)

            self.assertTrue(report["git_health"]["is_detached_head"])
            self.assertGreater(report["summary"]["detached_head"], 0)
            self.assertEqual(report["health_band"], HealthBand.MODERATE.value)
            self.assertTrue(any("detached" in d for d in report["diagnostics"]))

    # ---- FDX staleness detection ----

    def test_fdx_staleness_no_index(self):
        from cptr.services.workspace_health import _check_fdx_index_staleness

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            result = _check_fdx_index_staleness(repo)
            self.assertFalse(result["stale"])
            self.assertIsNone(result["index_path"])

    def test_fdx_staleness_stale_index(self):
        from cptr.services.workspace_health import _check_fdx_index_staleness
        import os

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            # Create .git/COMMIT_EDITMSG (newer)
            git_dir = repo / ".git"
            git_dir.mkdir()
            commit_file = git_dir / "COMMIT_EDITMSG"
            commit_file.write_text("initial commit", encoding="utf-8")

            # Create fdx index dir/file (older — set mtime to past)
            fdx_dir = repo / ".fdx"
            fdx_dir.mkdir()
            fdx_index = fdx_dir / "index"
            fdx_index.write_text("{}", encoding="utf-8")
            # Set fdx index mtime to 1000s in the past
            old_time = time.time() - 1000
            os.utime(str(fdx_index), (old_time, old_time))
            # Set commit mtime to now
            now_time = time.time()
            os.utime(str(commit_file), (now_time, now_time))

            result = _check_fdx_index_staleness(repo)
            self.assertTrue(result["stale"])
            self.assertIn("fdx", result["reason"])

    def test_fdx_staleness_fresh_index(self):
        from cptr.services.workspace_health import _check_fdx_index_staleness
        import os

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            git_dir = repo / ".git"
            git_dir.mkdir()
            commit_file = git_dir / "COMMIT_EDITMSG"
            commit_file.write_text("initial commit", encoding="utf-8")

            fdx_dir = repo / ".fdx"
            fdx_dir.mkdir()
            fdx_index = fdx_dir / "index"
            fdx_index.write_text("{}", encoding="utf-8")
            # Both at the same time — not stale
            now_time = time.time()
            os.utime(str(commit_file), (now_time, now_time))
            os.utime(str(fdx_index), (now_time, now_time))

            result = _check_fdx_index_staleness(repo)
            self.assertFalse(result["stale"])

    # ---- Unfinished command session detection ----

    def test_unfinished_sessions_detected(self):
        from cptr.services.workspace_health import _check_unfinished_command_sessions

        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            command_sessions["session_orphan"] = {
                "id": "session_orphan",
                "workspace": str(ws_root),
                "done": False,
                "created_at": time.time(),
            }
            result = _check_unfinished_command_sessions(ws_root)
            self.assertTrue(result["has_orphaned_sessions"])
            self.assertEqual(result["orphaned_count"], 1)
            self.assertIn("session_orphan", result["orphaned_session_ids"])

    def test_done_sessions_not_reported(self):
        from cptr.services.workspace_health import _check_unfinished_command_sessions

        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            command_sessions["session_done"] = {
                "id": "session_done",
                "workspace": str(ws_root),
                "done": True,
                "created_at": time.time(),
            }
            result = _check_unfinished_command_sessions(ws_root)
            self.assertFalse(result["has_orphaned_sessions"])
            self.assertEqual(result["orphaned_count"], 0)

    def test_other_workspace_sessions_not_reported(self):
        from cptr.services.workspace_health import _check_unfinished_command_sessions

        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp) / "mine"
            ws_root.mkdir()
            other_root = Path(temp) / "other"
            other_root.mkdir()
            command_sessions["session_other"] = {
                "id": "session_other",
                "workspace": str(other_root),
                "done": False,
                "created_at": time.time(),
            }
            result = _check_unfinished_command_sessions(ws_root)
            self.assertFalse(result["has_orphaned_sessions"])

    # ---- Duplicate workspace registration detection ----

    async def test_duplicate_workspace_registration_detected(self):
        from cptr.services.workspace_health import _check_duplicate_workspace_registration

        now_ms = int(time.time() * 1000)
        user_id = f"user_dup_{now_ms}"
        # Use a real path that resolves consistently
        with tempfile.TemporaryDirectory() as temp:
            path_a = str(Path(temp))  # both workspaces point to same resolved path
            ws_a_id = f"ws_dup_a_{now_ms}"
            ws_b_id = f"ws_dup_b_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=ws_a_id,
                        user_id=user_id,
                        path=path_a,
                        name="WS A",
                        data={},
                        created_at=now_ms,
                    )
                )
                # SQLite UniqueConstraint prevents exact path duplicate so we
                # simulate a resolved-path collision with a trailing slash variant
                ws_b_path = path_a.rstrip("/") + "/."  # different string, same resolved
                db.add(
                    Workspace(
                        id=ws_b_id,
                        user_id=user_id,
                        path=ws_b_path,
                        name="WS B",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            ws_a = Workspace(
                id=ws_a_id,
                user_id=user_id,
                path=path_a,
                name="WS A",
                data={},
                created_at=now_ms,
            )
            result = await _check_duplicate_workspace_registration(ws_a, user_id)
            self.assertTrue(result["duplicate"])
            self.assertIn(ws_b_id, result["duplicates"])

    async def test_no_duplicate_workspace_registration(self):
        from cptr.services.workspace_health import _check_duplicate_workspace_registration

        now_ms = int(time.time() * 1000)
        user_id = f"user_nodup_{now_ms}"
        with tempfile.TemporaryDirectory() as temp:
            path_a = str(Path(temp))
            ws_a_id = f"ws_nodup_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=ws_a_id,
                        user_id=user_id,
                        path=path_a,
                        name="Only WS",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            ws_a = Workspace(
                id=ws_a_id,
                user_id=user_id,
                path=path_a,
                name="Only WS",
                data={},
                created_at=now_ms,
            )
            result = await _check_duplicate_workspace_registration(ws_a, user_id)
            self.assertFalse(result["duplicate"])
            self.assertEqual(result["duplicates"], [])

    # ---- Origin/default-branch mismatch (unit-test the helper) ----

    async def test_origin_branch_check_detached_head(self):
        """When symbolic-ref fails (detached HEAD), mismatch is reported."""
        from unittest.mock import patch as mock_patch
        from cptr.services.workspace_health import _check_origin_default_branch_mismatch

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)

            # Simulate symbolic-ref returns non-zero (detached HEAD)
            async def fake_run(cmd, *args, cwd=None, check=True, identity=None, **kwargs):
                if cmd == "symbolic-ref":
                    return (1, "", "fatal: HEAD is a detached symbolic reference")
                return (0, "", "")

            with mock_patch("cptr.services.workspace_health._run", new=fake_run):
                result = await _check_origin_default_branch_mismatch(repo)

            self.assertTrue(result["mismatch"])
            self.assertIn("detached", result["reason"])

    async def test_origin_branch_check_matching(self):
        """When local branch matches origin default, mismatch is False."""
        from unittest.mock import patch as mock_patch
        from cptr.services.workspace_health import _check_origin_default_branch_mismatch

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)

            async def fake_run(cmd, *args, cwd=None, check=True, identity=None, **kwargs):
                if cmd == "symbolic-ref":
                    return (0, "main", "")
                if cmd == "ls-remote":
                    return (0, "ref: refs/heads/main\tHEAD\nabc123\tHEAD", "")
                return (0, "", "")

            with mock_patch("cptr.services.workspace_health._run", new=fake_run):
                result = await _check_origin_default_branch_mismatch(repo)

            self.assertFalse(result["mismatch"])
            self.assertEqual(result["local_branch"], "main")
            self.assertEqual(result["origin_default"], "main")

    async def test_origin_branch_check_mismatch(self):
        """When local branch differs from origin default, mismatch is True."""
        from unittest.mock import patch as mock_patch
        from cptr.services.workspace_health import _check_origin_default_branch_mismatch

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)

            async def fake_run(cmd, *args, cwd=None, check=True, identity=None, **kwargs):
                if cmd == "symbolic-ref":
                    return (0, "feature-branch", "")
                if cmd == "ls-remote":
                    return (0, "ref: refs/heads/main\tHEAD\nabc123\tHEAD", "")
                return (0, "", "")

            with mock_patch("cptr.services.workspace_health._run", new=fake_run):
                result = await _check_origin_default_branch_mismatch(repo)

            self.assertTrue(result["mismatch"])
            self.assertEqual(result["local_branch"], "feature-branch")
            self.assertEqual(result["origin_default"], "main")

    # ---- Additional hardened tests for Lane phase-16-health ----

    def test_noise_path_cptr_exact_and_nested(self):
        self.assertTrue(is_worktree_noise_path(".cptr"))
        self.assertTrue(is_worktree_noise_path(".cptr/skills/some-skill/SKILL.md"))
        self.assertTrue(is_worktree_noise_path(".cptr-worktrees"))
        self.assertTrue(is_worktree_noise_path(".cptr-worktrees/dcw_999/app.py"))

    def test_noise_path_subpath_of_known_absolute_path(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            wt = Path(temp) / "custom_worktrees" / "worker_x"
            wt.mkdir(parents=True)
            known = {str(wt)}
            # File inside that known worktree path
            self.assertTrue(
                is_worktree_noise_path(
                    str(wt / "nested" / "file.py"),
                    repo_root=repo,
                    known_worktree_paths=known,
                )
            )

    def test_noise_path_nested_worktree_gitdir_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            nested_wt = repo / "embedded_worker"
            nested_wt.mkdir()
            (nested_wt / ".git").write_text("gitdir: /some/path/to/gitdir\n", encoding="utf-8")
            (nested_wt / "deep" / "dir").mkdir(parents=True)
            deep_file = nested_wt / "deep" / "dir" / "file.py"
            deep_file.write_text("print(1)", encoding="utf-8")

            self.assertTrue(
                is_worktree_noise_path(
                    "embedded_worker/deep/dir/file.py",
                    repo_root=repo,
                )
            )

    async def test_noise_alone_leaves_health_healthy(self):
        """When git status only contains noise, health remains HEALTHY and is_clean_excluding_noise is True."""
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _init_repo(repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_noise_{now_ms}"
            workspace_id = f"ws_noise_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=user_id,
                        path=str(repo),
                        name="Noise Test",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            fake_status = {
                "branch": "main",
                "upstream": "",
                "ahead": 0,
                "behind": 0,
                "files": [{"path": ".cptr-worktrees/w1", "status": "untracked"}],
                "remote_url": "",
            }
            fake_wts = {"worktrees": []}

            with (
                patch(
                    "cptr.services.workspace_health.git_status",
                    new=AsyncMock(return_value=fake_status),
                ),
                patch(
                    "cptr.services.workspace_health.git_worktrees",
                    new=AsyncMock(return_value=fake_wts),
                ),
                patch(
                    "cptr.services.workspace_health._check_origin_default_branch_mismatch",
                    new=AsyncMock(return_value={"mismatch": False, "reason": "matches"}),
                ),
            ):
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(repo),
                    name="Noise Test",
                    data={},
                    created_at=now_ms,
                )
                report = await classify_workspace_health(workspace=ws, user_id=user_id)

            self.assertTrue(report["available"])
            self.assertEqual(report["health_band"], HealthBand.HEALTHY.value)
            self.assertTrue(report["git_health"]["worktree_noise"]["is_clean_excluding_noise"])
            self.assertFalse(report["git_health"]["worktree_noise"]["is_clean_strict"])
            self.assertEqual(report["summary"]["worktree_noise_count"], 1)
            self.assertEqual(report["summary"]["real_changes_count"], 0)

    async def test_canonical_dirty_degrades_health_to_moderate(self):
        """When genuine source files are dirty, health band becomes MODERATE."""
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _init_repo(repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_dirty_{now_ms}"
            workspace_id = f"ws_dirty_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=user_id,
                        path=str(repo),
                        name="Dirty Test",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            fake_status = {
                "branch": "main",
                "upstream": "",
                "ahead": 0,
                "behind": 0,
                "files": [
                    {"path": ".cptr-worktrees/w1", "status": "untracked"},
                    {"path": "src/main.py", "status": "modified"},
                ],
                "remote_url": "",
            }
            fake_wts = {"worktrees": []}

            with (
                patch(
                    "cptr.services.workspace_health.git_status",
                    new=AsyncMock(return_value=fake_status),
                ),
                patch(
                    "cptr.services.workspace_health.git_worktrees",
                    new=AsyncMock(return_value=fake_wts),
                ),
                patch(
                    "cptr.services.workspace_health._check_origin_default_branch_mismatch",
                    new=AsyncMock(return_value={"mismatch": False, "reason": "matches"}),
                ),
            ):
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(repo),
                    name="Dirty Test",
                    data={},
                    created_at=now_ms,
                )
                report = await classify_workspace_health(workspace=ws, user_id=user_id)

            self.assertEqual(report["health_band"], HealthBand.MODERATE.value)
            self.assertFalse(report["git_health"]["worktree_noise"]["is_clean_excluding_noise"])
            self.assertEqual(report["summary"]["worktree_noise_count"], 1)
            self.assertEqual(report["summary"]["real_changes_count"], 1)
            self.assertTrue(any("uncommitted source changes" in d for d in report["diagnostics"]))

    async def test_orphan_git_worktree_detected(self):
        """A registered git worktree not attached to any DB worker is flagged as orphan."""
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _init_repo(repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_orphan_{now_ms}"
            workspace_id = f"ws_orphan_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=user_id,
                        path=str(repo),
                        name="Orphan WT Test",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            fake_status = {
                "branch": "main",
                "upstream": "",
                "ahead": 0,
                "behind": 0,
                "files": [],
                "remote_url": "",
            }
            fake_wts = {
                "worktrees": [
                    {"path": str(repo), "branch": "main", "is_current": True, "is_detached": False},
                    {
                        "path": str(Path(temp) / "untracked_worktree"),
                        "branch": "cptr/direct/orphan",
                        "is_current": False,
                        "is_detached": False,
                    },
                ]
            }

            with (
                patch(
                    "cptr.services.workspace_health.git_status",
                    new=AsyncMock(return_value=fake_status),
                ),
                patch(
                    "cptr.services.workspace_health.git_worktrees",
                    new=AsyncMock(return_value=fake_wts),
                ),
                patch(
                    "cptr.services.workspace_health._check_origin_default_branch_mismatch",
                    new=AsyncMock(return_value={"mismatch": False, "reason": "matches"}),
                ),
            ):
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(repo),
                    name="Orphan WT Test",
                    data={},
                    created_at=now_ms,
                )
                report = await classify_workspace_health(workspace=ws, user_id=user_id)

            self.assertEqual(report["summary"]["orphan_worktrees"], 1)
            self.assertEqual(report["health_band"], HealthBand.MODERATE.value)
            self.assertTrue(any("orphan git worktree" in d for d in report["diagnostics"]))

    async def test_missing_git_worktree_checkout_detected(self):
        """A registered git worktree whose directory is missing or prunable is flagged."""
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _init_repo(repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_missing_wt_{now_ms}"
            workspace_id = f"ws_missing_wt_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=user_id,
                        path=str(repo),
                        name="Missing WT Test",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            missing_path = str(Path(temp) / "does_not_exist_wt")
            fake_status = {
                "branch": "main",
                "upstream": "",
                "ahead": 0,
                "behind": 0,
                "files": [],
                "remote_url": "",
            }
            fake_wts = {
                "worktrees": [
                    {"path": str(repo), "branch": "main", "is_current": True, "is_detached": False},
                    {
                        "path": missing_path,
                        "branch": "feature",
                        "is_current": False,
                        "is_detached": False,
                        "is_prunable": True,
                    },
                ]
            }

            with (
                patch(
                    "cptr.services.workspace_health.git_status",
                    new=AsyncMock(return_value=fake_status),
                ),
                patch(
                    "cptr.services.workspace_health.git_worktrees",
                    new=AsyncMock(return_value=fake_wts),
                ),
                patch(
                    "cptr.services.workspace_health._check_origin_default_branch_mismatch",
                    new=AsyncMock(return_value={"mismatch": False, "reason": "matches"}),
                ),
            ):
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(repo),
                    name="Missing WT Test",
                    data={},
                    created_at=now_ms,
                )
                report = await classify_workspace_health(workspace=ws, user_id=user_id)

            self.assertEqual(report["summary"]["missing_git_worktrees"], 1)
            self.assertEqual(report["health_band"], HealthBand.MODERATE.value)
            self.assertTrue(any("missing or moved" in d for d in report["diagnostics"]))

    def test_fdx_staleness_with_worktree_dot_git_file(self):
        """_check_fdx_index_staleness resolves gitdir in a linked worktree."""
        from cptr.services.workspace_health import _check_fdx_index_staleness

        with tempfile.TemporaryDirectory() as temp:
            base_git = Path(temp) / "main_repo" / ".git"
            base_git.mkdir(parents=True)
            commit_file = base_git / "COMMIT_EDITMSG"
            commit_file.write_text("commit msg", encoding="utf-8")

            wt_dir = Path(temp) / "worktree"
            wt_dir.mkdir()
            (wt_dir / ".git").write_text(f"gitdir: {base_git}\n", encoding="utf-8")

            fdx_dir = wt_dir / ".fdx"
            fdx_dir.mkdir()
            fdx_index = fdx_dir / "index"
            fdx_index.write_text("{}", encoding="utf-8")

            # Make fdx index older
            old_time = time.time() - 100
            os.utime(str(fdx_index), (old_time, old_time))
            now_time = time.time()
            os.utime(str(commit_file), (now_time, now_time))

            result = _check_fdx_index_staleness(wt_dir)
            self.assertTrue(result["stale"])

    async def test_origin_branch_check_no_origin_remote(self):
        """When origin remote is not configured, mismatch is False."""
        from cptr.services.workspace_health import _check_origin_default_branch_mismatch

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)

            async def fake_run(cmd, *args, cwd=None, check=True, identity=None, **kwargs):
                if cmd == "symbolic-ref" and "refs/remotes/origin/HEAD" in args:
                    return (1, "", "fatal: ref refs/remotes/origin/HEAD is not a symbolic ref")
                if cmd == "symbolic-ref":
                    return (0, "main", "")
                if cmd == "ls-remote":
                    return (1, "", "fatal: 'origin' does not appear to be a git repository")
                if cmd == "remote":
                    return (0, "", "")
                return (0, "", "")

            with patch("cptr.services.workspace_health._run", new=fake_run):
                result = await _check_origin_default_branch_mismatch(repo)
            self.assertFalse(result["mismatch"])
            self.assertIn("no origin remote configured", result["reason"])

    async def test_origin_branch_check_fallback_to_local_remote_head(self):
        """When ls-remote fails but local refs/remotes/origin/HEAD is present, it uses it."""
        from cptr.services.workspace_health import _check_origin_default_branch_mismatch

        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)

            async def fake_run(cmd, *args, cwd=None, check=True, identity=None, **kwargs):
                if cmd == "symbolic-ref" and "refs/remotes/origin/HEAD" in args:
                    return (0, "origin/main\n", "")
                if cmd == "symbolic-ref":
                    return (0, "main\n", "")
                if cmd == "ls-remote":
                    return (1, "", "network timeout")
                return (0, "", "")

            with patch("cptr.services.workspace_health._run", new=fake_run):
                result = await _check_origin_default_branch_mismatch(repo)
            self.assertFalse(result["mismatch"])
            self.assertEqual(result["origin_default"], "main")
            self.assertEqual(result["local_branch"], "main")

    def test_unfinished_sessions_in_workspace_subdirectories(self):
        """Command sessions started inside workspace subdirectories are identified."""
        from cptr.services.workspace_health import _check_unfinished_command_sessions

        with tempfile.TemporaryDirectory() as temp:
            ws_root = Path(temp)
            subdir = ws_root / "subdir" / "project"
            subdir.mkdir(parents=True)
            command_sessions["session_nested"] = {
                "id": "session_nested",
                "workspace": str(subdir),
                "done": False,
                "created_at": time.time(),
            }
            result = _check_unfinished_command_sessions(ws_root)
            self.assertTrue(result["has_orphaned_sessions"])
            self.assertEqual(result["orphaned_count"], 1)
            self.assertIn("session_nested", result["orphaned_session_ids"])


class RouterHealthAndReconcileEndpointsTests(_HealthDbCase):
    """End-to-end API router tests for /coding/health, /coding/reconcile, and /coding/inspect (health)."""

    def setUp(self):
        command_sessions.clear()

    def tearDown(self):
        command_sessions.clear()

    async def test_router_coding_health_and_reconcile_endpoints(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from cptr.routers.coding import router as coding_router

        app = FastAPI()
        app.include_router(coding_router)

        with tempfile.TemporaryDirectory() as temp:
            base_repo = Path(temp) / "repo"
            base_repo.mkdir()
            _init_repo(base_repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_api_{now_ms}"
            workspace_id = f"ws_api_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                workspace = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(base_repo),
                    name="API Health Test",
                    data={},
                    created_at=now_ms,
                )
                db.add(workspace)
                await db.commit()

            with (
                patch("cptr.routers.coding._user", new=AsyncMock(return_value=user_id)),
                patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
                patch("cptr.routers.coding.identity_for_request", new=AsyncMock(return_value=None)),
                TestClient(app) as client,
            ):
                # 1. GET /workspaces/{id}/coding/health
                res_health = client.get(f"/api/control/v1/workspaces/{workspace_id}/coding/health")
                self.assertEqual(res_health.status_code, 200)
                data_health = res_health.json()
                self.assertEqual(data_health["workspace_id"], workspace_id)
                self.assertTrue(data_health["available"])
                self.assertIn("health_band", data_health)
                self.assertIn("summary", data_health)

                # 2. POST /workspaces/{id}/coding/inspect (kind="health")
                res_inspect = client.post(
                    f"/api/control/v1/workspaces/{workspace_id}/coding/inspect",
                    json={"kind": "health"},
                )
                self.assertEqual(res_inspect.status_code, 200)
                data_inspect = res_inspect.json()
                self.assertEqual(data_inspect["kind"], "health")
                self.assertEqual(data_inspect["workspace_id"], workspace_id)
                self.assertEqual(data_inspect["health_band"], data_health["health_band"])

                # 3. POST /workspaces/{id}/coding/reconcile
                res_reconcile = client.post(
                    f"/api/control/v1/workspaces/{workspace_id}/coding/reconcile"
                )
                self.assertEqual(res_reconcile.status_code, 200)
                data_reconcile = res_reconcile.json()
                self.assertEqual(data_reconcile["workspace_id"], workspace_id)
                self.assertFalse(data_reconcile["discarded"])
                self.assertEqual(data_reconcile["deleted_worktrees_count"], 0)
                self.assertEqual(data_reconcile["deleted_branches_count"], 0)
                self.assertIn("health", data_reconcile)


class CptrGeneratedStatePost5356492Tests(_HealthDbCase):
    """Prove CPTR generated-state rules: untracked .fdx trees are CPTR-owned derived state

    and treated as generated noise, while tracked changes under .fdx fail closed.
    """

    def setUp(self):
        command_sessions.clear()

    def tearDown(self):
        command_sessions.clear()

    def test_noise_path_untracked_cptr_fdx_state(self):
        self.assertTrue(is_worktree_noise_path(".fdx", status="untracked"))
        self.assertTrue(is_worktree_noise_path(".fdx/index.sqlite", status="untracked"))
        self.assertTrue(is_worktree_noise_path(".fdx/tee/search.log", status="untracked"))
        self.assertTrue(is_worktree_noise_path(".fdx-index", status="untracked"))

    def test_noise_path_tracked_fdx_fails_closed(self):
        # Tracked modifications or additions under .fdx fail closed
        self.assertFalse(is_worktree_noise_path(".fdx/index.sqlite", status="modified"))
        self.assertFalse(is_worktree_noise_path(".fdx/index.sqlite", status="added"))
        self.assertFalse(is_worktree_noise_path(".fdx/index.sqlite"))

    def test_classify_worktree_noise_allows_untracked_fdx(self):
        files = [
            {"path": ".fdx/index.sqlite", "status": "untracked"},
            {"path": ".fdx/tee/search.log", "status": "untracked"},
        ]
        result = classify_worktree_noise(files)
        self.assertTrue(result["has_noise"])
        self.assertEqual(
            result["noise_paths"],
            [".fdx/index.sqlite", ".fdx/tee/search.log"],
        )
        self.assertEqual(result["real_changes"], [])
        self.assertFalse(result["is_clean_strict"])
        self.assertTrue(result["is_clean_excluding_noise"])

    def test_classify_worktree_noise_tracked_fdx_fails_closed(self):
        files = [
            {"path": ".fdx/index.sqlite", "status": "modified"},
        ]
        result = classify_worktree_noise(files)
        self.assertEqual(result["noise_paths"], [])
        self.assertEqual(result["real_changes"], [".fdx/index.sqlite"])
        self.assertFalse(result["is_clean_strict"])
        self.assertFalse(result["is_clean_excluding_noise"])

    async def test_classify_workspace_health_untracked_fdx_remains_healthy(self):
        """Untracked .fdx does not degrade workspace health; health band remains HEALTHY."""
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _init_repo(repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_fdx_{now_ms}"
            workspace_id = f"ws_fdx_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=user_id,
                        path=str(repo),
                        name="FDX Noise Test",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            fake_status = {
                "branch": "main",
                "upstream": "",
                "ahead": 0,
                "behind": 0,
                "files": [
                    {"path": ".fdx/index.sqlite", "status": "untracked"},
                    {"path": ".fdx/tee/search.log", "status": "untracked"},
                ],
                "remote_url": "",
            }
            fake_wts = {"worktrees": []}

            with (
                patch(
                    "cptr.services.workspace_health.git_status",
                    new=AsyncMock(return_value=fake_status),
                ),
                patch(
                    "cptr.services.workspace_health.git_worktrees",
                    new=AsyncMock(return_value=fake_wts),
                ),
                patch(
                    "cptr.services.workspace_health._check_origin_default_branch_mismatch",
                    new=AsyncMock(return_value={"mismatch": False, "reason": "matches"}),
                ),
            ):
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(repo),
                    name="FDX Noise Test",
                    data={},
                    created_at=now_ms,
                )
                report = await classify_workspace_health(workspace=ws, user_id=user_id)

            self.assertTrue(report["available"])
            self.assertEqual(report["health_band"], HealthBand.HEALTHY.value)
            self.assertTrue(report["git_health"]["worktree_noise"]["is_clean_excluding_noise"])
            self.assertFalse(report["git_health"]["worktree_noise"]["is_clean_strict"])
            self.assertEqual(report["summary"]["worktree_noise_count"], 2)
            self.assertEqual(report["summary"]["real_changes_count"], 0)

    async def test_classify_workspace_health_tracked_fdx_fails_closed_to_moderate(self):
        """Tracked .fdx modification fails closed and degrades health to MODERATE."""
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            _init_repo(repo)

            now_ms = int(time.time() * 1000)
            user_id = f"user_fdx_mod_{now_ms}"
            workspace_id = f"ws_fdx_mod_{now_ms}"

            async with await get_db() as db:
                from cptr.models import User

                db.add(User(id=user_id, role="user", settings={}, created_at=now_ms))
                await db.flush()
                db.add(
                    Workspace(
                        id=workspace_id,
                        user_id=user_id,
                        path=str(repo),
                        name="Tracked FDX Test",
                        data={},
                        created_at=now_ms,
                    )
                )
                await db.commit()

            fake_status = {
                "branch": "main",
                "upstream": "",
                "ahead": 0,
                "behind": 0,
                "files": [
                    {"path": ".fdx/index.sqlite", "status": "modified"},
                ],
                "remote_url": "",
            }
            fake_wts = {"worktrees": []}

            with (
                patch(
                    "cptr.services.workspace_health.git_status",
                    new=AsyncMock(return_value=fake_status),
                ),
                patch(
                    "cptr.services.workspace_health.git_worktrees",
                    new=AsyncMock(return_value=fake_wts),
                ),
                patch(
                    "cptr.services.workspace_health._check_origin_default_branch_mismatch",
                    new=AsyncMock(return_value={"mismatch": False, "reason": "matches"}),
                ),
            ):
                ws = Workspace(
                    id=workspace_id,
                    user_id=user_id,
                    path=str(repo),
                    name="Tracked FDX Test",
                    data={},
                    created_at=now_ms,
                )
                report = await classify_workspace_health(workspace=ws, user_id=user_id)

            self.assertEqual(report["health_band"], HealthBand.MODERATE.value)
            self.assertFalse(report["git_health"]["worktree_noise"]["is_clean_excluding_noise"])
            self.assertEqual(report["summary"]["worktree_noise_count"], 0)
            self.assertEqual(report["summary"]["real_changes_count"], 1)
