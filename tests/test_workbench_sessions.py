import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, User, WorkbenchSession, WorkbenchSessionEvent
from cptr.services.workbench_sessions import WorkbenchSessionStore
from cptr.routers.workbench import (
    AppendWorkbenchSessionEventRequest,
    BindWorkbenchSessionTargetRequest,
    CreateWorkbenchSessionRequest,
    RenameWorkbenchSessionRequest,
    append_workbench_session_event,
    bind_workbench_session,
    create_workbench_session,
    get_workbench_session_events,
    rename_workbench_session,
    router as workbench_router,
)


class WorkbenchSessionStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_command_releases_only_matching_active_target_and_keeps_workbench_open(
        self,
    ):
        claimed = SimpleNamespace(id="wbs_command", event_count=5)
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        db.execute.return_value = SimpleNamespace(all=lambda: [claimed])
        added = []
        db.add = Mock(side_effect=added.append)

        with patch(
            "cptr.services.workbench_sessions.get_db",
            new=AsyncMock(return_value=db),
        ):
            changed = await WorkbenchSessionStore().reconcile_command_terminal(
                owner_id="user_1",
                workspace_id="ws_1",
                command_id="cmd_1",
                status="COMPLETE",
                exit_code=0,
            )

        self.assertEqual(changed, 1)
        self.assertEqual(len(added), 1)
        event = added[0]
        self.assertEqual(event.sequence, 5)
        self.assertEqual(event.event_type, "command.completed")
        self.assertEqual(event.state, "COMPLETE")
        self.assertEqual(event.target_id, "cmd_1")
        self.assertEqual(event.details, {"exit_code": 0})
        db.commit.assert_awaited_once()

    async def test_idle_workbench_records_are_archived_and_unbound_without_touching_execution(self):
        session = SimpleNamespace(
            id="wbs_stale",
            status="RUNNING",
            active_target_type="command",
            active_target_id="cmd_old",
            active_workspace_id="ws_old",
            updated_at=1,
            archived_at=None,
            deleted_at=None,
        )
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        db.scalars.return_value = SimpleNamespace(all=lambda: [session])

        with patch(
            "cptr.services.workbench_sessions.get_db",
            new=AsyncMock(return_value=db),
        ):
            changed = await WorkbenchSessionStore().archive_stale(
                idle_seconds=60,
                now_ms=120_000,
            )

        self.assertEqual(changed, 1)
        self.assertEqual(session.status, "ARCHIVED")
        self.assertEqual(session.archived_at, 120_000)
        self.assertEqual(session.updated_at, 120_000)
        self.assertIsNone(session.active_target_type)
        self.assertIsNone(session.active_target_id)
        self.assertIsNone(session.active_workspace_id)
        db.commit.assert_awaited_once()

    async def test_reconcile_restart_clears_transient_command_and_preserves_sticky_workspace(self):
        session = SimpleNamespace(
            id="wbs_restart",
            user_id="user_1",
            name="Restart Session",
            workspace_id="ws_durable",
            status="RUNNING",
            active_target_type="command",
            active_target_id="cmd_running",
            active_workspace_id="ws_transient",
            event_count=2,
            environment_profile_id="env_durable",
            environment_profile_override={"KEY": "VAL"},
            admin_role="admin_durable",
            role_context={"perm": "all"},
            last_context_snapshot_id="snap_durable",
            created_at=1,
            updated_at=10,
            last_event_at=10,
            archived_at=None,
            deleted_at=None,
        )
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        db.scalars.return_value = SimpleNamespace(all=lambda: [session])
        added = []
        db.add = Mock(side_effect=added.append)

        with patch(
            "cptr.services.workbench_sessions.get_db",
            new=AsyncMock(return_value=db),
        ):
            changed = await WorkbenchSessionStore().reconcile_restart(now_ms=50_000)

        self.assertEqual(changed, 1)
        self.assertEqual(session.status, "OPEN")
        self.assertIsNone(session.active_target_type)
        self.assertIsNone(session.active_target_id)
        self.assertIsNone(session.active_workspace_id)
        self.assertEqual(session.workspace_id, "ws_durable")
        self.assertEqual(session.environment_profile_id, "env_durable")
        self.assertEqual(session.environment_profile_override, {"KEY": "VAL"})
        self.assertEqual(session.admin_role, "admin_durable")
        self.assertEqual(session.role_context, {"perm": "all"})
        self.assertEqual(session.last_context_snapshot_id, "snap_durable")
        self.assertEqual(len(added), 1)
        event = added[0]
        self.assertEqual(event.event_type, "workbench.restart_reconciled")
        self.assertEqual(event.state, "OPEN")
        self.assertEqual(event.target_type, "command")
        self.assertEqual(event.target_id, "cmd_running")
        self.assertEqual(event.workspace_id, "ws_transient")
        db.commit.assert_awaited_once()

    async def test_manual_archive_clears_active_target_projection(self):
        session = SimpleNamespace(
            id="wbs_archive",
            user_id="user_1",
            name="Archive",
            workspace_id="ws_1",
            status="RUNNING",
            active_target_type="command",
            active_target_id="cmd_done",
            active_workspace_id="ws_1",
            event_count=3,
            created_at=1,
            updated_at=10,
            last_event_at=10,
            archived_at=None,
            deleted_at=None,
        )
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        db.scalar.return_value = session

        with patch(
            "cptr.services.workbench_sessions.get_db",
            new=AsyncMock(return_value=db),
        ):
            result = await WorkbenchSessionStore().archive(
                owner_id="user_1",
                session_id="wbs_archive",
            )

        self.assertEqual(result["status"], "ARCHIVED")
        self.assertIsNone(result["active_target_type"])
        self.assertIsNone(result["active_target_id"])
        self.assertIsNone(result["active_workspace_id"])
        self.assertIsNone(session.active_target_type)
        self.assertIsNone(session.active_target_id)
        self.assertIsNone(session.active_workspace_id)

    async def test_binding_new_target_reopens_terminal_session_as_running(self):
        session = SimpleNamespace(
            id="wbs_rebind",
            user_id="user_1",
            name="Rebind",
            workspace_id="ws_1",
            status="COMPLETE",
            active_target_type="command",
            active_target_id="old_cmd",
            active_workspace_id="ws_1",
            event_count=1,
            created_at=1,
            updated_at=10,
            last_event_at=10,
            archived_at=None,
            deleted_at=None,
        )
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        db.scalar.return_value = session

        with patch(
            "cptr.services.workbench_sessions.get_db",
            new=AsyncMock(return_value=db),
        ):
            result = await WorkbenchSessionStore().bind_target(
                owner_id="user_1",
                session_id="wbs_rebind",
                target_type="command",
                target_id="new_cmd",
                workspace_id="ws_1",
            )

        self.assertEqual(result["status"], "RUNNING")
        self.assertEqual(session.status, "RUNNING")
        self.assertEqual(session.active_target_id, "new_cmd")
        db.commit.assert_awaited_once()

    async def test_command_started_event_normalizes_stale_terminal_state_to_running(self):
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        claim_result = Mock()
        claim_result.one_or_none.return_value = SimpleNamespace(
            id="wbs_event",
            event_count=3,
            active_target_type=None,
            active_target_id=None,
        )
        db.execute.return_value = claim_result
        added = []
        db.add = Mock(side_effect=added.append)

        with patch(
            "cptr.services.workbench_sessions.get_db",
            new=AsyncMock(return_value=db),
        ):
            event = await WorkbenchSessionStore().append_event(
                owner_id="user_1",
                session_id="wbs_event",
                event_type="command.started",
                summary="started",
                state="COMPLETE",
                target_type="command",
                target_id="cmd_new",
                workspace_id="ws_1",
            )

        self.assertEqual(event["state"], "RUNNING")
        self.assertEqual(event["sequence"], 3)
        self.assertEqual(added[0].state, "RUNNING")
        self.assertEqual(db.execute.await_count, 2)

    async def test_terminal_event_only_releases_matching_active_target(self):
        db = AsyncMock()
        db.__aenter__.return_value = db
        db.__aexit__.return_value = False
        first_claim = Mock()
        first_claim.one_or_none.return_value = SimpleNamespace(
            id="wbs_overlap",
            event_count=5,
            active_target_type="command",
            active_target_id="cmd_new",
        )
        second_claim = Mock()
        second_claim.one_or_none.return_value = SimpleNamespace(
            id="wbs_overlap",
            event_count=6,
            active_target_type="command",
            active_target_id="cmd_new",
        )
        db.execute.side_effect = [first_claim, second_claim, Mock()]
        db.add = Mock()

        store = WorkbenchSessionStore()
        with patch(
            "cptr.services.workbench_sessions.get_db",
            new=AsyncMock(return_value=db),
        ):
            old_event = await store.append_event(
                owner_id="user_1",
                session_id="wbs_overlap",
                event_type="command.completed",
                summary="old complete",
                state="COMPLETE",
                target_type="command",
                target_id="cmd_old",
                workspace_id="ws_1",
            )
            self.assertEqual(old_event["state"], "COMPLETE")

            matched_event = await store.append_event(
                owner_id="user_1",
                session_id="wbs_overlap",
                event_type="command.completed",
                summary="new complete",
                state="COMPLETE",
                target_type="command",
                target_id="cmd_new",
                workspace_id="ws_1",
            )

        self.assertEqual(matched_event["state"], "COMPLETE")
        self.assertEqual(old_event["sequence"], 5)
        self.assertEqual(matched_event["sequence"], 6)
        self.assertEqual(db.execute.await_count, 3)


class _TwoPartyBarrier:
    def __init__(self):
        self._count = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def wait(self):
        async with self._lock:
            self._count += 1
            if self._count >= 2:
                self._ready.set()
        await self._ready.wait()


class _RaceSession:
    """Synchronize the first DB operation without changing transaction semantics."""

    def __init__(self, session, barrier: _TwoPartyBarrier):
        self._session = session
        self._barrier = barrier
        self._synchronized = False

    async def __aenter__(self):
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self._session.__aexit__(*args)

    async def _before_write(self):
        if self._synchronized:
            return
        self._synchronized = True
        await self._barrier.wait()

    async def _after_read(self):
        if self._synchronized:
            return
        self._synchronized = True
        await self._barrier.wait()

    async def execute(self, statement, *args, **kwargs):
        await self._before_write()
        return await self._session.execute(statement, *args, **kwargs)

    async def scalar(self, statement, *args, **kwargs):
        value = await self._session.scalar(statement, *args, **kwargs)
        await self._after_read()
        return value

    async def scalars(self, statement, *args, **kwargs):
        value = await self._session.scalars(statement, *args, **kwargs)
        await self._after_read()
        return value

    def add(self, value):
        self._session.add(value)

    async def commit(self):
        return await self._session.commit()

    async def refresh(self, value):
        return await self._session.refresh(value)


class WorkbenchSessionConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self.temp.name) / 'workbench-race.db'}"
        )

        @event.listens_for(self.engine.sync_engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA journal_mode=WAL")
            finally:
                cursor.close()

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.factory() as db:
            db.add(User(id="user-race", role="user", settings={}, created_at=1))
            await db.commit()
            db.add(
                WorkbenchSession(
                    id="wbs_race",
                    user_id="user-race",
                    name="Race",
                    status="OPEN",
                    event_count=0,
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.temp.cleanup()

    async def test_concurrent_appenders_allocate_distinct_sequences_atomically(self):
        barrier = _TwoPartyBarrier()

        async def race_db():
            return _RaceSession(self.factory(), barrier)

        store = WorkbenchSessionStore()
        with patch("cptr.services.workbench_sessions.get_db", new=race_db):
            results = await asyncio.gather(
                store.append_event(
                    owner_id="user-race",
                    session_id="wbs_race",
                    event_type="race.a",
                    summary="a",
                ),
                store.append_event(
                    owner_id="user-race",
                    session_id="wbs_race",
                    event_type="race.b",
                    summary="b",
                ),
            )

        self.assertEqual(sorted(result["sequence"] for result in results), [1, 2])
        async with self.factory() as db:
            session = await db.get(WorkbenchSession, "wbs_race")
            events = (
                await db.scalars(
                    select(WorkbenchSessionEvent)
                    .where(WorkbenchSessionEvent.session_id == "wbs_race")
                    .order_by(WorkbenchSessionEvent.sequence)
                )
            ).all()
        self.assertEqual(session.event_count, 2)
        self.assertEqual([event.sequence for event in events], [1, 2])

    async def test_append_and_terminal_reconcile_share_one_atomic_sequence_allocator(self):
        async with self.factory() as db:
            session = await db.get(WorkbenchSession, "wbs_race")
            session.status = "RUNNING"
            session.active_target_type = "command"
            session.active_target_id = "cmd_race"
            session.active_workspace_id = "ws_race"
            await db.commit()

        barrier = _TwoPartyBarrier()

        async def race_db():
            return _RaceSession(self.factory(), barrier)

        store = WorkbenchSessionStore()
        with patch("cptr.services.workbench_sessions.get_db", new=race_db):
            appended, reconciled = await asyncio.gather(
                store.append_event(
                    owner_id="user-race",
                    session_id="wbs_race",
                    event_type="mcp.tool",
                    summary="parallel observability event",
                ),
                store.reconcile_command_terminal(
                    owner_id="user-race",
                    workspace_id="ws_race",
                    command_id="cmd_race",
                    status="COMPLETE",
                    exit_code=0,
                ),
            )

        self.assertEqual(reconciled, 1)
        self.assertIn(appended["sequence"], {1, 2})
        async with self.factory() as db:
            session = await db.get(WorkbenchSession, "wbs_race")
            events = (
                await db.scalars(
                    select(WorkbenchSessionEvent)
                    .where(WorkbenchSessionEvent.session_id == "wbs_race")
                    .order_by(WorkbenchSessionEvent.sequence)
                )
            ).all()
        self.assertEqual(session.event_count, 2)
        self.assertEqual(session.status, "OPEN")
        self.assertIsNone(session.active_target_type)
        self.assertIsNone(session.active_target_id)
        self.assertIsNone(session.active_workspace_id)
        self.assertEqual([event.sequence for event in events], [1, 2])
        self.assertEqual({event.event_type for event in events}, {"mcp.tool", "command.completed"})


class WorkbenchSessionRouterTests(unittest.IsolatedAsyncioTestCase):
    def test_production_app_registers_current_plugin_session_routes(self):
        paths = {route.path for route in workbench_router.routes if hasattr(route, "path")}
        self.assertIn("/api/control/v1/workbench-sessions", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}/events", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}/bind", paths)
        self.assertIn("/api/control/v1/workbench-sessions/{session_id}/delete-request", paths)
        self.assertIn("/api/control/v1/workbench-sessions/delete-confirm", paths)

    async def test_create_session_records_safe_open_event(self):
        request = SimpleNamespace()
        initial = {
            "session_id": "wbs_1",
            "name": "Release work",
            "workspace_id": "ws_1",
            "status": "OPEN",
            "event_count": 0,
        }
        current = {**initial, "event_count": 1}
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.workbench._ensure_workspace_owner",
                new=AsyncMock(return_value=object()),
            ),
            patch(
                "cptr.routers.workbench.workbench_session_store.create",
                new=AsyncMock(return_value=initial),
            ) as create,
            patch(
                "cptr.routers.workbench.workbench_session_store.append_event",
                new=AsyncMock(return_value={"sequence": 1}),
            ) as append,
            patch(
                "cptr.routers.workbench.workbench_session_store.get",
                new=AsyncMock(return_value=current),
            ),
        ):
            result = await create_workbench_session(
                request,
                CreateWorkbenchSessionRequest(name="Release work", workspace_id="ws_1"),
            )

        self.assertEqual(result["event_count"], 1)
        create.assert_awaited_once_with(owner_id="user_1", name="Release work", workspace_id="ws_1")
        self.assertEqual(append.await_args.kwargs["event_type"], "workbench.opened")

    async def test_bind_reconciles_command_that_completed_before_workbench_binding(self):
        request = SimpleNamespace()
        bound = {
            "session_id": "wbs_1",
            "status": "RUNNING",
            "active_target_type": "command",
            "active_target_id": "cmd_fast",
            "active_workspace_id": "ws_1",
        }
        reconciled = {
            **bound,
            "status": "OPEN",
            "active_target_type": None,
            "active_target_id": None,
        }
        command = {
            "done": True,
            "exit_code": 0,
            "live_target": {
                "target_type": "command",
                "target_id": "cmd_fast",
                "workspace_id": "ws_1",
            },
        }
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.workbench._ensure_target_owner", new=AsyncMock(return_value=None)),
            patch("cptr.routers.workbench.get_command_session", return_value=command),
            patch(
                "cptr.routers.workbench.workbench_session_store.bind_target",
                new=AsyncMock(return_value=bound),
            ),
            patch(
                "cptr.routers.workbench.workbench_session_store.append_event",
                new=AsyncMock(return_value={"sequence": 2}),
            ),
            patch(
                "cptr.routers.workbench.workbench_session_store.reconcile_command_terminal",
                new=AsyncMock(return_value=1),
            ) as reconcile,
            patch(
                "cptr.routers.workbench.workbench_session_store.get",
                new=AsyncMock(return_value=reconciled),
            ),
        ):
            result = await bind_workbench_session(
                request,
                "wbs_1",
                BindWorkbenchSessionTargetRequest(
                    target_type="command",
                    target_id="cmd_fast",
                    workspace_id="ws_1",
                ),
            )

        reconcile.assert_awaited_once_with(
            owner_id="user_1",
            workspace_id="ws_1",
            command_id="cmd_fast",
            status="COMPLETE",
            exit_code=0,
        )
        self.assertEqual(result["status"], "OPEN")
        self.assertIsNone(result["active_target_type"])

    async def test_late_command_started_event_reconciles_already_completed_command(self):
        request = SimpleNamespace()
        command = {
            "done": True,
            "exit_code": 0,
            "live_target": {
                "target_type": "command",
                "target_id": "cmd_fast",
                "workspace_id": "ws_1",
            },
        }
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.workbench._ensure_target_owner", new=AsyncMock(return_value=None)),
            patch("cptr.routers.workbench.get_command_session", return_value=command),
            patch(
                "cptr.routers.workbench.workbench_session_store.append_event",
                new=AsyncMock(return_value={"sequence": 3, "state": "RUNNING"}),
            ),
            patch(
                "cptr.routers.workbench.workbench_session_store.reconcile_command_terminal",
                new=AsyncMock(return_value=1),
            ) as reconcile,
        ):
            result = await append_workbench_session_event(
                request,
                "wbs_1",
                AppendWorkbenchSessionEventRequest(
                    event_type="command.started",
                    summary="ChatGPT started a CPTR workspace command.",
                    state="RUNNING",
                    target_type="command",
                    target_id="cmd_fast",
                    workspace_id="ws_1",
                    tool_name="cptr_code_run_command",
                ),
            )

        self.assertEqual(result["sequence"], 3)
        reconcile.assert_awaited_once_with(
            owner_id="user_1",
            workspace_id="ws_1",
            command_id="cmd_fast",
            status="COMPLETE",
            exit_code=0,
        )

    async def test_command_binding_joins_workbench_to_original_command_trace(self):
        request = SimpleNamespace(
            headers={
                "x-cptr-trace-id": "trace-bind-call",
                "x-cptr-request-id": "request-bind",
                "x-cptr-tool-name": "cptr_bind_live_workbench_session",
            }
        )
        bound = {
            "session_id": "wbs_trace",
            "status": "RUNNING",
            "active_target_type": "command",
            "active_target_id": "cmd_trace",
            "active_workspace_id": "ws_1",
        }
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.workbench._ensure_target_owner", new=AsyncMock(return_value=None)),
            patch(
                "cptr.routers.workbench.workbench_session_store.bind_target",
                new=AsyncMock(return_value=bound),
            ),
            patch(
                "cptr.routers.workbench.workbench_session_store.append_event",
                new=AsyncMock(return_value={"sequence": 2}),
            ),
            patch(
                "cptr.routers.workbench.workbench_session_store.get",
                new=AsyncMock(return_value=bound),
            ),
            patch(
                "cptr.routers.workbench.action_trace_store.resolve_entity",
                new=AsyncMock(return_value="trace-command-origin"),
            ) as resolve_trace,
            patch(
                "cptr.routers.workbench.action_trace_store.link_entity",
                new=AsyncMock(return_value=True),
            ) as link_trace,
            patch(
                "cptr.routers.workbench.action_trace_store.append",
                new=AsyncMock(return_value=True),
            ) as append_trace,
        ):
            result = await bind_workbench_session(
                request,
                "wbs_trace",
                BindWorkbenchSessionTargetRequest(
                    target_type="command",
                    target_id="cmd_trace",
                    workspace_id="ws_1",
                ),
            )

        self.assertEqual(result["session_id"], "wbs_trace")
        resolve_trace.assert_awaited_once_with(
            owner_id="user_1", entity_type="command", entity_id="cmd_trace"
        )
        link_trace.assert_awaited_once_with(
            owner_id="user_1",
            trace_id="trace-command-origin",
            entity_type="workbench",
            entity_id="wbs_trace",
        )
        self.assertEqual(append_trace.await_args.kwargs["trace_id"], "trace-command-origin")
        self.assertEqual(append_trace.await_args.kwargs["layer"], "workbench")
        self.assertEqual(append_trace.await_args.kwargs["name"], "workbench.target.bound")

    async def test_events_return_last_sequence_cursor_expected_by_plugin(self):
        request = SimpleNamespace()
        events = [{"sequence": 3, "summary": "done"}]
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.workbench.workbench_session_store.events",
                new=AsyncMock(return_value=events),
            ),
        ):
            result = await get_workbench_session_events(
                request, "wbs_1", after_sequence=2, limit=20
            )

        self.assertEqual(result["last_sequence"], 3)
        self.assertEqual(result["events"], events)

    async def test_rename_matches_current_patch_contract(self):
        request = SimpleNamespace()
        renamed = {"session_id": "wbs_1", "name": "New name", "status": "OPEN"}
        with (
            patch("cptr.routers.workbench._user", new=AsyncMock(return_value="user_1")),
            patch(
                "cptr.routers.workbench.workbench_session_store.rename",
                new=AsyncMock(return_value=renamed),
            ) as rename,
        ):
            result = await rename_workbench_session(
                request, "wbs_1", RenameWorkbenchSessionRequest(name="New name")
            )

        self.assertEqual(result["name"], "New name")
        rename.assert_awaited_once_with(owner_id="user_1", session_id="wbs_1", name="New name")
