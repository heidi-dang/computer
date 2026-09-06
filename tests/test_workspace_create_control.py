import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, ControlIdempotency, User, Workspace
from cptr.routers.control import (
    WorkspaceCreateRequest,
    _workspace_create_request_fingerprint,
    create_workspace,
    router as control_router,
)
from cptr.utils import git as git_utils
from cptr.utils.runtime import FileError, Runtime


class WorkspaceCreateControlContractTests(unittest.TestCase):
    def test_control_router_registers_one_workspace_create_endpoint(self):
        routes = [
            route
            for route in control_router.routes
            if route.path == "/api/control/v1/workspaces"
            and "POST" in (route.methods or set())
        ]
        self.assertEqual(len(routes), 1)


class GitInitHelperTests(unittest.IsolatedAsyncioTestCase):
    async def test_init_repo_uses_identity_aware_git_runner(self):
        self.assertTrue(hasattr(git_utils, "init_repo"))
        runner = AsyncMock(return_value=(0, "", ""))
        identity = SimpleNamespace(is_pam=False)
        with patch.object(git_utils, "_run", new=runner):
            await git_utils.init_repo("/canonical/new", identity)
        runner.assert_awaited_once_with("init", cwd="/canonical/new", identity=identity)


class WorkspaceCreateControlBehaviorTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))

    async def test_existing_registered_directory_is_idempotent(self):
        existing = SimpleNamespace(
            id="ws-existing",
            user_id="user-1",
            path="/canonical/demo",
            name="Existing Demo",
            data={},
        )
        identity = SimpleNamespace(home="/home/tester")
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=AsyncMock(return_value="/canonical/demo"), create=True),
            patch.object(Runtime, "stat", new=AsyncMock(return_value={"type": "directory"})),
            patch.object(Workspace, "get_by_path", new=AsyncMock(return_value=existing)),
            patch.object(Workspace, "upsert", new=AsyncMock()) as upsert,
            patch("cptr.routers.control.identity_for_request", new=AsyncMock(return_value=identity), create=True),
            patch("cptr.routers.control.is_repo", new=AsyncMock(return_value=False), create=True),
        ):
            result = await create_workspace(
                self.request(),
                WorkspaceCreateRequest(path="/canonical/demo", name="Demo"),
            )

        self.assertEqual(result["workspace_id"], "ws-existing")
        self.assertEqual(result["name"], "Existing Demo")
        self.assertFalse(result["created_workspace"])
        self.assertFalse(result["created_directory"])
        self.assertFalse(result["initialized_git"])
        self.assertTrue(result["available"])
        self.assertFalse(result["is_git_repo"])
        upsert.assert_not_awaited()

    async def test_archived_workspace_is_restored_without_creating_a_duplicate(self):
        existing = SimpleNamespace(
            id="ws-archived",
            user_id="user-1",
            path="/canonical/archived",
            name="Archived Demo",
            data={"_cptr_archived": True, "note": "keep"},
        )
        restored = SimpleNamespace(
            id="ws-archived",
            user_id="user-1",
            path="/canonical/archived",
            name="Archived Demo",
            data={"note": "keep"},
        )
        identity = SimpleNamespace(home="/home/tester")
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=AsyncMock(return_value="/canonical/archived")),
            patch.object(Runtime, "stat", new=AsyncMock(return_value={"type": "directory"})),
            patch.object(Workspace, "get_by_path", new=AsyncMock(return_value=existing)),
            patch.object(Workspace, "upsert", new=AsyncMock(return_value=restored)) as upsert,
            patch("cptr.routers.control.identity_for_request", new=AsyncMock(return_value=identity)),
            patch("cptr.routers.control.is_repo", new=AsyncMock(return_value=True)),
        ):
            result = await create_workspace(
                self.request(),
                WorkspaceCreateRequest(path="/canonical/archived"),
            )

        upsert.assert_awaited_once_with(
            "user-1",
            "/canonical/archived",
            "Archived Demo",
            {"note": "keep"},
        )
        self.assertFalse(result["created_workspace"])
        self.assertTrue(result["restored_workspace"])
        self.assertEqual(result["workspace_id"], "ws-archived")

    async def test_missing_directory_requires_explicit_create_flag(self):
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=AsyncMock(return_value="/canonical/missing"), create=True),
            patch.object(Runtime, "stat", new=AsyncMock(side_effect=FileError("not found", 404))),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_workspace(
                    self.request(),
                    WorkspaceCreateRequest(path="/canonical/missing"),
                )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_explicit_directory_creation_and_git_init_use_identity_aware_services(self):
        created = SimpleNamespace(
            id="ws-new",
            user_id="user-1",
            path="/canonical/new",
            name="New Demo",
            data={},
        )
        identity = SimpleNamespace(home="/home/tester")
        stat = AsyncMock(side_effect=[FileError("not found", 404), {"type": "directory"}])
        create_item = AsyncMock()
        init_repo = AsyncMock()
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=AsyncMock(return_value="/canonical/new"), create=True),
            patch.object(Runtime, "stat", new=stat),
            patch.object(Runtime, "create_item", new=create_item),
            patch.object(Workspace, "get_by_path", new=AsyncMock(return_value=None)),
            patch.object(Workspace, "upsert", new=AsyncMock(return_value=created)) as upsert,
            patch("cptr.routers.control.identity_for_request", new=AsyncMock(return_value=identity), create=True),
            patch("cptr.routers.control.is_repo", new=AsyncMock(side_effect=[False, True]), create=True),
            patch("cptr.routers.control.init_repo", new=init_repo, create=True),
        ):
            result = await create_workspace(
                self.request(),
                WorkspaceCreateRequest(
                    path="/canonical/new",
                    name="New Demo",
                    create_directory=True,
                    initialize_git=True,
                ),
            )

        create_item.assert_awaited_once_with(self.request(), "/canonical/new", type="directory")
        init_repo.assert_awaited_once_with("/canonical/new", identity)
        upsert.assert_awaited_once_with("user-1", "/canonical/new", "New Demo", {})
        self.assertEqual(result["workspace_id"], "ws-new")
        self.assertTrue(result["created_workspace"])
        self.assertTrue(result["created_directory"])
        self.assertTrue(result["initialized_git"])
        self.assertTrue(result["is_git_repo"])

    async def test_existing_non_directory_path_is_rejected(self):
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=AsyncMock(return_value="/canonical/file"), create=True),
            patch.object(Runtime, "stat", new=AsyncMock(return_value={"type": "file"})),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_workspace(
                    self.request(),
                    WorkspaceCreateRequest(path="/canonical/file", create_directory=True),
                )
        self.assertEqual(raised.exception.status_code, 409)


class WorkspaceCreateIdempotencyPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id="user-1", role="admin", settings={}, created_at=1))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _db(self):
        return self.sessions()

    def request(self):
        return SimpleNamespace(state=SimpleNamespace(), app=SimpleNamespace(state=SimpleNamespace()))

    async def test_same_idempotency_key_replays_durable_workspace_response_without_reexecution(self):
        created = SimpleNamespace(
            id="ws-idempotent",
            user_id="user-1",
            path="/canonical/idempotent",
            name="Idempotent Demo",
            data={},
        )
        identity = SimpleNamespace(home="/home/tester")
        resolve = AsyncMock(return_value="/canonical/idempotent")
        stat = AsyncMock(return_value={"type": "directory"})
        upsert = AsyncMock(return_value=created)
        with (
            patch("cptr.routers.control.get_db", new=AsyncMock(side_effect=self._db)),
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=resolve),
            patch.object(Runtime, "stat", new=stat),
            patch.object(Workspace, "get_by_path", new=AsyncMock(return_value=None)),
            patch.object(Workspace, "upsert", new=upsert),
            patch("cptr.routers.control.identity_for_request", new=AsyncMock(return_value=identity)),
            patch("cptr.routers.control.is_repo", new=AsyncMock(return_value=False)),
        ):
            body = WorkspaceCreateRequest(
                path="/canonical/idempotent",
                name="Idempotent Demo",
                idempotency_key="workspace-replay-1",
            )
            first = await create_workspace(self.request(), body)
            second = await create_workspace(self.request(), body)

        self.assertEqual(second, first)
        resolve.assert_awaited_once()
        stat.assert_awaited_once()
        upsert.assert_awaited_once()
        async with self.sessions() as db:
            record = (
                await db.execute(
                    select(ControlIdempotency).where(
                        ControlIdempotency.user_id == "user-1",
                        ControlIdempotency.key == "workspace-create:workspace-replay-1",
                    )
                )
            ).scalar_one_or_none()
        self.assertIsNotNone(record)
        self.assertEqual(record.resource_type, "workspace")
        self.assertEqual(record.resource_id, "ws-idempotent")

    async def test_idempotency_key_collision_fails_closed_before_filesystem_mutation(self):
        async with self.sessions() as db:
            db.add(
                ControlIdempotency(
                    user_id="user-1",
                    key="workspace-create:collision-key",
                    resource_type="direct_command",
                    resource_id="command-1",
                    response={"command_id": "command-1"},
                    created_at=1,
                )
            )
            await db.commit()

        resolve = AsyncMock(return_value="/canonical/collision")
        stat = AsyncMock(return_value={"type": "directory"})
        with (
            patch("cptr.routers.control.get_db", new=AsyncMock(side_effect=self._db)),
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=resolve),
            patch.object(Runtime, "stat", new=stat),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_workspace(
                    self.request(),
                    WorkspaceCreateRequest(
                        path="/canonical/collision",
                        idempotency_key="collision-key",
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        resolve.assert_not_awaited()
        stat.assert_not_awaited()

    async def test_same_idempotency_key_with_different_request_fails_closed_before_mutation(self):
        original = WorkspaceCreateRequest(
            path="/canonical/original",
            name="Original",
            idempotency_key="mismatch-key",
        )
        fingerprint = _workspace_create_request_fingerprint(original)
        original_result = {
            "workspace_id": "ws-original",
            "name": "Original",
            "available": True,
            "is_git_repo": False,
            "created_workspace": True,
            "restored_workspace": False,
            "created_directory": False,
            "initialized_git": False,
        }
        async with self.sessions() as db:
            db.add(
                ControlIdempotency(
                    user_id="user-1",
                    key="workspace-create:mismatch-key",
                    resource_type="workspace",
                    resource_id="ws-original",
                    response={
                        "request_fingerprint": fingerprint,
                        "result": original_result,
                    },
                    created_at=1,
                )
            )
            await db.commit()

        resolve = AsyncMock(return_value="/canonical/different")
        stat = AsyncMock(return_value={"type": "directory"})
        with (
            patch("cptr.routers.control.get_db", new=AsyncMock(side_effect=self._db)),
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.control._resolve_request_workspace_path", new=resolve),
            patch.object(Runtime, "stat", new=stat),
        ):
            with self.assertRaises(HTTPException) as raised:
                await create_workspace(
                    self.request(),
                    WorkspaceCreateRequest(
                        path="/canonical/different",
                        name="Different",
                        idempotency_key="mismatch-key",
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail.get("code"),
            "WORKSPACE_IDEMPOTENCY_REQUEST_MISMATCH",
        )
        resolve.assert_not_awaited()
        stat.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
