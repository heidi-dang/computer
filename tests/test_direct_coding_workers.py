import asyncio
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.routers.coding import EditRequest, edit_workspace_file
from cptr.services.direct_coding_workers import (
    DirectCodingWorkerError,
    DirectCodingWorkerService,
    _idempotent_worker_id,
    apply_worker_changes,
    create_worker_worktree,
    worker_changed_paths,
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


def _repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "CPTR Tests")
    (root / "app.py").write_text("value = 1\n", encoding="utf-8")
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "initial")


class DirectCodingWorkerMechanicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_worktree_is_isolated_from_source_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "repo"
            base.mkdir()
            _repo(base)
            worker_root = Path(temp) / "workers" / "backend"

            created = await create_worker_worktree(
                source_root=base,
                worker_root=worker_root,
                branch="cptr/direct/test-backend",
            )
            Path(created).joinpath("app.py").write_text("value = 2\n", encoding="utf-8")

            self.assertEqual((base / "app.py").read_text(encoding="utf-8"), "value = 1\n")
            self.assertEqual(Path(created, "app.py").read_text(encoding="utf-8"), "value = 2\n")
            self.assertEqual(await worker_changed_paths(Path(created)), {"app.py"})

    async def test_worker_creation_rejects_a_dirty_source_repository(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "repo"
            base.mkdir()
            _repo(base)
            (base / "app.py").write_text("dirty = True\n", encoding="utf-8")

            with self.assertRaises(DirectCodingWorkerError) as caught:
                await create_worker_worktree(
                    source_root=base,
                    worker_root=Path(temp) / "workers" / "dirty",
                    branch="cptr/direct/test-dirty",
                )

            self.assertEqual(caught.exception.code, "DIRECT_WORKER_DIRTY_BASE")

    async def test_worker_creation_allows_untracked_cptr_fdx_state(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "repo"
            base.mkdir()
            _repo(base)
            fdx = base / ".fdx"
            fdx.mkdir()
            (fdx / "index.sqlite").write_bytes(b"derived")
            (fdx / "tee").mkdir()
            (fdx / "tee" / "search.log").write_text("derived\n", encoding="utf-8")

            created = await create_worker_worktree(
                source_root=base,
                worker_root=Path(temp) / "workers" / "fdx",
                branch="cptr/direct/test-fdx",
            )

            self.assertTrue(Path(created).is_dir())
            self.assertFalse(Path(created, ".fdx").exists())

    async def test_worker_creation_rejects_untracked_non_cptr_state(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "repo"
            base.mkdir()
            _repo(base)
            (base / "notes.txt").write_text("user work\n", encoding="utf-8")

            with self.assertRaises(DirectCodingWorkerError) as caught:
                await create_worker_worktree(
                    source_root=base,
                    worker_root=Path(temp) / "workers" / "dirty-untracked",
                    branch="cptr/direct/test-dirty-untracked",
                )

            self.assertEqual(caught.exception.code, "DIRECT_WORKER_DIRTY_BASE")

    async def test_integration_copies_non_overlapping_worker_changes_without_committing(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "repo"
            base.mkdir()
            _repo(base)
            worker_root = Path(temp) / "workers" / "backend"
            created = Path(
                await create_worker_worktree(
                    source_root=base,
                    worker_root=worker_root,
                    branch="cptr/direct/test-integrate",
                )
            )
            (created / "app.py").write_text("value = 3\n", encoding="utf-8")
            (created / "new.py").write_text("new = True\n", encoding="utf-8")

            result = await apply_worker_changes(base, created)

            self.assertEqual(result["conflicts"], [])
            self.assertEqual(set(result["applied_paths"]), {"app.py", "new.py"})
            self.assertEqual((base / "app.py").read_text(encoding="utf-8"), "value = 3\n")
            self.assertEqual((base / "new.py").read_text(encoding="utf-8"), "new = True\n")
            self.assertTrue(
                _git(base, "status", "--porcelain"), "integration must remain uncommitted"
            )

    async def test_integration_refuses_overlapping_base_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp) / "repo"
            base.mkdir()
            _repo(base)
            created = Path(
                await create_worker_worktree(
                    source_root=base,
                    worker_root=Path(temp) / "workers" / "backend",
                    branch="cptr/direct/test-conflict",
                )
            )
            (created / "app.py").write_text("worker = True\n", encoding="utf-8")
            (base / "app.py").write_text("base = True\n", encoding="utf-8")

            result = await apply_worker_changes(base, created)

            self.assertEqual(result["applied_paths"], [])
            self.assertEqual(result["conflicts"], ["app.py"])
            self.assertEqual((base / "app.py").read_text(encoding="utf-8"), "base = True\n")


class DirectCodingWorkerIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_idempotency_key_reuses_existing_worker_without_new_worktree(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = SimpleNamespace(id="ws-1", path=temp)
            service = DirectCodingWorkerService()
            worker_id = _idempotent_worker_id(
                user_id="user-1",
                workspace_id="ws-1",
                key="fanout-1:branch-1",
            )
            existing = SimpleNamespace(
                id=worker_id,
                user_id="user-1",
                workspace_id="ws-1",
                name="Native ChatGPT Subagent 01",
                responsibility="audit auth",
                repo_path=".",
                status="READY",
                branch=f"cptr/direct/{worker_id}",
                worktree_path=temp,
                base_revision="abc123",
                created_at=1,
                updated_at=1,
                last_activity_at=1,
                integrated_at=None,
                closed_at=None,
            )
            service._get = AsyncMock(return_value=existing)
            service.summary = AsyncMock(return_value={"worker_id": worker_id})
            with (
                patch(
                    "cptr.services.direct_coding_workers.identity_for_user_id",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "cptr.services.direct_coding_workers.is_repo",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "cptr.services.direct_coding_workers.repository_root",
                    new=AsyncMock(return_value=temp),
                ),
                patch(
                    "cptr.services.direct_coding_workers.create_worker_worktree",
                    new=AsyncMock(),
                ) as create_worktree,
            ):
                result = await service.create(
                    user_id="user-1",
                    workspace=workspace,
                    name="Native ChatGPT Subagent 01",
                    responsibility="audit auth",
                    repo_path=".",
                    idempotency_key="fanout-1:branch-1",
                )

            self.assertEqual(result["worker_id"], worker_id)
            create_worktree.assert_not_awaited()

    async def test_idempotent_create_lock_serializes_concurrent_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = SimpleNamespace(id="ws-1", path=temp)
            service = DirectCodingWorkerService()
            worker_id = _idempotent_worker_id(
                user_id="user-1",
                workspace_id="ws-1",
                key="fanout-2:branch-1",
            )
            existing = SimpleNamespace(
                id=worker_id,
                user_id="user-1",
                workspace_id="ws-1",
                name="Native ChatGPT Subagent 01",
                responsibility="audit tests",
                repo_path=".",
                status="READY",
                branch=f"cptr/direct/{worker_id}",
                worktree_path=temp,
                base_revision="abc123",
                created_at=1,
                updated_at=1,
                last_activity_at=1,
                integrated_at=None,
                closed_at=None,
            )
            active = 0
            peak = 0

            async def get_existing(**_kwargs):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0)
                active -= 1
                return existing

            service._get = AsyncMock(side_effect=get_existing)
            service.summary = AsyncMock(return_value={"worker_id": worker_id})
            with (
                patch(
                    "cptr.services.direct_coding_workers.identity_for_user_id",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "cptr.services.direct_coding_workers.is_repo",
                    new=AsyncMock(return_value=True),
                ),
                patch(
                    "cptr.services.direct_coding_workers.repository_root",
                    new=AsyncMock(return_value=temp),
                ),
                patch(
                    "cptr.services.direct_coding_workers.create_worker_worktree",
                    new=AsyncMock(),
                ) as create_worktree,
            ):
                first, second = await asyncio.gather(
                    service.create(
                        user_id="user-1",
                        workspace=workspace,
                        name="Native ChatGPT Subagent 01",
                        responsibility="audit tests",
                        idempotency_key="fanout-2:branch-1",
                    ),
                    service.create(
                        user_id="user-1",
                        workspace=workspace,
                        name="Native ChatGPT Subagent 01",
                        responsibility="audit tests",
                        idempotency_key="fanout-2:branch-1",
                    ),
                )

            self.assertEqual(first, second)
            self.assertEqual(peak, 1)
            create_worktree.assert_not_awaited()


class DirectCodingWorkerRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_edit_uses_worker_root_when_worker_id_is_supplied(self):
        request = SimpleNamespace()
        workspace = SimpleNamespace(path="/tmp/source")
        body = EditRequest(
            worker_id="dcw_backend",
            path="src/app.py",
            target="old",
            replacement="new",
        )
        with (
            patch("cptr.routers.coding._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.coding._workspace", new=AsyncMock(return_value=workspace)),
            patch(
                "cptr.routers.coding.resolve_direct_worker_root",
                new=AsyncMock(return_value=Path("/tmp/worktree").resolve()),
            ) as resolve_worker,
            patch(
                "cptr.routers.coding.Runtime.read_file",
                new=AsyncMock(return_value={"binary": False, "content": "old\n"}),
            ),
            patch(
                "cptr.routers.coding.Runtime.write_file", new=AsyncMock(return_value={})
            ) as write_file,
        ):
            await edit_workspace_file(request, "ws_1", body)

        resolve_worker.assert_awaited_once_with(
            user_id="user_1",
            workspace_id="ws_1",
            worker_id="dcw_backend",
        )
        write_file.assert_awaited_once_with(request, "/tmp/worktree/src/app.py", "new\n")


if __name__ == "__main__":
    unittest.main()
