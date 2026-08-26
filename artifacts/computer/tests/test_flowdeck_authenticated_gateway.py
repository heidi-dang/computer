import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from cptr.flowdeck.authenticated_gateway import (
    AuthenticatedGatewayError,
    SpecialistDispatchRequest,
    dispatch_authenticated_specialist,
)
from cptr.flowdeck.terminal_observer import recent_terminal_frames
from cptr.flowdeck.coding import CodingRequest, run_browser_debugger, run_coding_specialist
from cptr.flowdeck.config import FlowDeckMode
from cptr.flowdeck.durable import DurableFlowDeck
from cptr.flowdeck.execution import MapperRequest, run_read_only_specialist
from cptr.models.base import Base
from cptr.models.workspaces import Workspace
from cptr.utils.config import AuthResult
from cptr.utils.tools import execute_tool


def auth_request(user_id=None):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/__internal__",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("internal", 0),
            "scheme": "http",
            "state": {},
        }
    )
    if user_id:
        request.state.auth = AuthResult(user_id=user_id, role="user")
    return request


class AuthenticatedGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        with tempfile.NamedTemporaryFile(delete=False) as db:
            self.db_path = db.name
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False)
        )
        async with self.store.session_factory() as session:
            session.add(
                Workspace(
                    user_id="owner",
                    path=str(self.root),
                    name="owned",
                    data={},
                    created_at=1,
                )
            )
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_path)
        self.temp.cleanup()

    def dispatch(self, role="debug-specialist", workspace=None):
        return SpecialistDispatchRequest(
            role=role,
            request_key=f"gateway-{role}",
            task="inspect",
            workspace=str(workspace or self.root),
            model="free",
            connection={},
            parent_chat_id="parent",
        )

    async def test_missing_wrong_and_forged_identity_fail_closed(self):
        for request, workspace in (
            (auth_request(), self.root),
            (auth_request("other"), self.root),
            (auth_request("owner"), self.root / "forged"),
        ):
            with self.assertRaises(AuthenticatedGatewayError):
                await dispatch_authenticated_specialist(
                    request, self.dispatch(workspace=workspace), store=self.store
                )

    async def test_stale_and_ambiguous_ownership_fail_closed(self):
        async with self.store.session_factory() as session:
            await session.execute(
                __import__("sqlalchemy").delete(Workspace).where(Workspace.user_id == "owner")
            )
            await session.commit()
        with self.assertRaises(AuthenticatedGatewayError):
            await dispatch_authenticated_specialist(
                auth_request("owner"), self.dispatch(), store=self.store
            )

    async def test_gateway_derives_identity_and_dispatches_read_only_role(self):
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": FlowDeckMode.READ_ONLY.value,
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch(
                "cptr.utils.tools._create_subagent_chat",
                new=AsyncMock(
                    return_value=(
                        type("Chat", (), {"id": "chat"})(),
                        None,
                        type("Message", (), {"id": "message"})(),
                    )
                ),
            ),
            patch(
                "cptr.utils.tools._run_existing_subagent_chat",
                new=AsyncMock(return_value="read-only result"),
            ),
        ):
            result = await dispatch_authenticated_specialist(
                auth_request("owner"), self.dispatch(), store=self.store
            )
        self.assertEqual(result, "read-only result")

    async def test_all_supported_roles_use_the_authenticated_gateway(self):
        roles = (
            "backend-coder",
            "frontend-coder",
            "browser-debugger",
            "debug-specialist",
            "mapper",
            "researcher",
            "architect",
            "reviewer",
            "security-auditor",
        )
        with patch.dict(
            os.environ,
            {
                "CPTR_FLOWDECK_ENABLED": "true",
                "CPTR_FLOWDECK_MODE": "controlled",
                "CPTR_FLOWDECK_GOVERNANCE": "strict",
                "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                "CPTR_FLOWDECK_CODING_ROLE": "backend-coder",
            },
            clear=False,
        ), patch(
            "cptr.flowdeck.authenticated_gateway._native_run_coding_specialist",
            new=AsyncMock(return_value="coder"),
        ), patch(
            "cptr.flowdeck.authenticated_gateway._native_run_browser_debugger",
            new=AsyncMock(return_value="browser"),
        ), patch(
            "cptr.flowdeck.authenticated_gateway._native_run_read_only_specialist",
            new=AsyncMock(return_value="readonly"),
        ):
            for role in roles:
                result = await dispatch_authenticated_specialist(
                    auth_request("owner"), self.dispatch(role=role), store=self.store
                )
                self.assertIn(result, {"coder", "browser", "readonly"})

    async def test_coding_dispatch_can_use_only_an_authenticated_repository_worktree(self):
        import subprocess
        from dataclasses import replace

        subprocess.check_call(("git", "-C", str(self.root), "init", "-q"))
        subprocess.check_call(("git", "-C", str(self.root), "config", "user.name", "test"))
        subprocess.check_call(("git", "-C", str(self.root), "config", "user.email", "test@example.invalid"))
        (self.root / "README.md").write_text("base\n")
        subprocess.check_call(("git", "-C", str(self.root), "add", "README.md"))
        subprocess.check_call(("git", "-C", str(self.root), "commit", "-qm", "base"))
        from cptr.flowdeck.worktrees import create_worktree, remove_worktree

        handle = await create_worktree(
            canonical_workspace=str(self.root),
            run_id="gateway",
            node_key="backend",
        )
        try:
            with patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                    "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                    "CPTR_FLOWDECK_CODING_ROLE": "backend-coder",
                },
                clear=False,
            ), patch(
                "cptr.flowdeck.authenticated_gateway._native_run_coding_specialist",
                new=AsyncMock(return_value="coder"),
            ) as native:
                result = await dispatch_authenticated_specialist(
                    auth_request("owner"),
                    replace(
                        self.dispatch(role="backend-coder"),
                        execution_workspace=handle.path,
                    ),
                    store=self.store,
                )
            self.assertEqual(result, "coder")
            request = native.call_args.args[0]
            self.assertEqual(request.workspace, handle.path)
            self.assertEqual(request.canonical_workspace, str(self.root))
        finally:
            await remove_worktree(handle)

        with self.assertRaises(AuthenticatedGatewayError):
            await dispatch_authenticated_specialist(
                auth_request("owner"),
                replace(
                    self.dispatch(role="backend-coder"),
                    execution_workspace=str(Path(self.temp.name).parent),
                ),
                store=self.store,
            )

    async def test_authenticated_coding_child_streams_terminal_frames_until_finalization(self):
        request = auth_request("owner")
        fake_chat = type("Chat", (), {"id": "terminal-coding-chat"})()
        fake_message = type("Message", (), {"id": "terminal-coding-message"})()
        child_kwargs = {}

        async def native_loop(*_args, **kwargs):
            child_kwargs.update(kwargs)
            emitted = []

            async def emit(**data):
                emitted.append(data)

            async def observe(kind, payload):
                from cptr.flowdeck.terminal_observer import emit_terminal_frame

                await emit_terminal_frame(
                    user_id="owner",
                    emit=emit,
                    kind=kind,
                    payload=payload,
                    run_id=kwargs["flowdeck_run_id"],
                )

            context = {
                "workspace": kwargs["workspace"],
                "user_id": "owner",
                "request": kwargs["request"],
                "specialist_role": kwargs["specialist_role"],
                "allowed_tool_names": kwargs["allowed_tool_names"],
                "tool_guard": kwargs["tool_guard"],
                "before_mutation": kwargs["before_mutation"],
                "after_mutation": kwargs["after_mutation"],
                "flowdeck_run_id": kwargs["flowdeck_run_id"],
                "flowdeck_attempt_id": kwargs["flowdeck_attempt_id"],
                "flowdeck_store": self.store,
                "terminal_observer": observe,
            }
            result = await execute_tool(
                "agent_terminal_command",
                {"command": "printf 'coding terminal\\n'"},
                context,
            )
            self.assertIn('"status": "succeeded"', result)
            self.assertTrue(emitted)
            return result

        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                    "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                    "CPTR_FLOWDECK_AGENT_TERMINAL_ENABLED": "true",
                    "CPTR_FLOWDECK_CODING_ROLE": "backend-coder",
                },
                clear=False,
            ),
            patch(
                "cptr.utils.tools._create_subagent_chat",
                new=AsyncMock(return_value=(fake_chat, None, fake_message)),
            ),
            patch(
                "cptr.utils.tools._run_existing_subagent_chat",
                new=native_loop,
            ),
        ):
            result = await dispatch_authenticated_specialist(
                request,
                self.dispatch(role="backend-coder"),
                store=self.store,
            )

        self.assertIn('"status": "succeeded"', result)
        self.assertIs(child_kwargs["request"], request)
        run = await self.store.get_run_by_request_key("gateway-backend-coder")
        self.assertIsNotNone(run)
        self.assertEqual(run.status, "SUCCEEDED")

        frames = recent_terminal_frames(run.id)
        command_frames = [
            frame
            for frame in frames
            if frame["frame_kind"] in {"command_start", "command_output", "command_exit"}
        ]
        self.assertGreaterEqual(len(command_frames), 3)
        frame_kinds = [frame["frame_kind"] for frame in command_frames]
        start_index = frame_kinds.index("command_start")
        output_index = frame_kinds.index("command_output")
        exit_index = len(frame_kinds) - 1 - frame_kinds[::-1].index("command_exit")
        self.assertEqual(start_index, 0)
        self.assertLess(start_index, output_index)
        self.assertLess(output_index, exit_index)
        self.assertEqual(exit_index, len(frame_kinds) - 1)
        self.assertEqual(
            command_frames[0]["payload"]["tool_name"],
            "agent_terminal_command",
        )
        self.assertEqual(
            command_frames[-1]["payload"]["exit_code"],
            0,
        )
        self.assertIn(
            "coding terminal",
            next(
                frame["payload"]["text"]
                for frame in command_frames
                if frame["frame_kind"] == "command_output"
            ),
        )

        with patch.dict(
            os.environ,
            {
                "CPTR_FLOWDECK_ENABLED": "true",
                "CPTR_FLOWDECK_MODE": "controlled",
                "CPTR_FLOWDECK_GOVERNANCE": "strict",
                "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                "CPTR_FLOWDECK_AGENT_TERMINAL_ENABLED": "true",
            },
            clear=False,
        ):
            after_finalize = await execute_tool(
                "agent_terminal_command",
                {"command": "printf should-not-run"},
                {
                    "workspace": str(self.root),
                    "user_id": "owner",
                    "request": request,
                    "specialist_role": "backend-coder",
                    "allowed_tool_names": frozenset({"agent_terminal_command"}),
                    "tool_guard": lambda name, args, context: True,
                    "flowdeck_run_id": run.id,
                    "flowdeck_store": self.store,
                },
            )
        self.assertIn("no longer active", after_finalize.lower())

    async def test_separate_authenticated_coding_runs_receive_isolated_terminals_and_frames(self):
        from dataclasses import replace

        request = auth_request("owner")
        (self.root / "first-run-only").mkdir()
        fake_chat = type("Chat", (), {"id": "isolated-terminal-chat"})()
        fake_message = type("Message", (), {"id": "isolated-terminal-message"})()
        invocations = []

        async def native_loop(*_args, **kwargs):
            run_index = len(invocations)
            emitted = []

            async def emit(**data):
                emitted.append(data)

            async def observe(kind, payload):
                from cptr.flowdeck.terminal_observer import emit_terminal_frame

                await emit_terminal_frame(
                    user_id="owner",
                    emit=emit,
                    kind=kind,
                    payload=payload,
                    run_id=kwargs["flowdeck_run_id"],
                )

            context = {
                "workspace": kwargs["workspace"],
                "user_id": "owner",
                "request": kwargs["request"],
                "specialist_role": kwargs["specialist_role"],
                "allowed_tool_names": kwargs["allowed_tool_names"],
                "tool_guard": kwargs["tool_guard"],
                "before_mutation": kwargs["before_mutation"],
                "after_mutation": kwargs["after_mutation"],
                "flowdeck_run_id": kwargs["flowdeck_run_id"],
                "flowdeck_attempt_id": kwargs["flowdeck_attempt_id"],
                "flowdeck_store": self.store,
                "terminal_observer": observe,
            }
            command = (
                "cd first-run-only && printf 'first-run-only\\n'"
                if run_index == 0
                else "pwd && printf 'second-run-only\\n'"
            )
            result = await execute_tool(
                "agent_terminal_command",
                {"command": command},
                context,
            )
            invocations.append(
                {
                    "run_id": kwargs["flowdeck_run_id"],
                    "result": json.loads(result),
                    "emitted": emitted,
                }
            )
            return result

        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                    "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                    "CPTR_FLOWDECK_AGENT_TERMINAL_ENABLED": "true",
                    "CPTR_FLOWDECK_CODING_ROLE": "backend-coder",
                },
                clear=False,
            ),
            patch(
                "cptr.utils.tools._create_subagent_chat",
                new=AsyncMock(return_value=(fake_chat, None, fake_message)),
            ),
            patch(
                "cptr.utils.tools._run_existing_subagent_chat",
                new=native_loop,
            ),
        ):
            await dispatch_authenticated_specialist(
                request,
                replace(
                    self.dispatch(role="backend-coder"),
                    request_key="gateway-backend-coder-isolation-first",
                ),
                store=self.store,
            )
            await dispatch_authenticated_specialist(
                request,
                replace(
                    self.dispatch(role="backend-coder"),
                    request_key="gateway-backend-coder-isolation-second",
                ),
                store=self.store,
            )

        self.assertEqual(len(invocations), 2)
        first_run = await self.store.get_run_by_request_key(
            "gateway-backend-coder-isolation-first"
        )
        second_run = await self.store.get_run_by_request_key(
            "gateway-backend-coder-isolation-second"
        )
        self.assertIsNotNone(first_run)
        self.assertIsNotNone(second_run)
        self.assertNotEqual(first_run.id, second_run.id)
        self.assertEqual(first_run.status, "SUCCEEDED")
        self.assertEqual(second_run.status, "SUCCEEDED")

        first_result = invocations[0]["result"]
        second_result = invocations[1]["result"]
        self.assertEqual(first_result["status"], "succeeded")
        self.assertEqual(second_result["status"], "succeeded")
        self.assertNotEqual(first_result["session_id"], second_result["session_id"])
        self.assertIn("first-run-only", first_result["output"])
        self.assertIn(str(self.root), second_result["output"])
        self.assertIn("second-run-only", second_result["output"])
        self.assertNotIn("first-run-only", second_result["output"])

        first_all_frames = recent_terminal_frames(first_run.id)
        second_all_frames = recent_terminal_frames(second_run.id)
        first_frames = [
            frame
            for frame in first_all_frames
            if frame["frame_kind"] in {"command_start", "command_output", "command_exit"}
        ]
        second_frames = [
            frame
            for frame in second_all_frames
            if frame["frame_kind"] in {"command_start", "command_output", "command_exit"}
        ]
        self.assertGreaterEqual(len(first_frames), 3)
        self.assertGreaterEqual(len(second_frames), 3)
        first_sessions = {
            frame["payload"]["session_id"] for frame in first_frames
        }
        second_sessions = {
            frame["payload"]["session_id"] for frame in second_frames
        }
        self.assertEqual(first_sessions, {first_result["session_id"]})
        self.assertEqual(second_sessions, {second_result["session_id"]})
        self.assertTrue(first_sessions.isdisjoint(second_sessions))
        self.assertTrue(
            all(frame["terminal_run_id"] == first_run.id for frame in first_frames)
        )
        self.assertTrue(
            all(frame["terminal_run_id"] == second_run.id for frame in second_frames)
        )
        first_text = " ".join(
            frame["payload"].get("text", "")
            for frame in first_frames
            if frame["frame_kind"] == "command_output"
        )
        second_text = " ".join(
            frame["payload"].get("text", "")
            for frame in second_frames
            if frame["frame_kind"] == "command_output"
        )
        self.assertIn("first-run-only", first_text)
        self.assertNotIn("second-run-only", first_text)
        self.assertIn("second-run-only", second_text)
        self.assertNotIn("first-run-only", second_text)
        self.assertEqual(first_all_frames[0]["sequence"], 1)
        self.assertEqual(second_all_frames[0]["sequence"], 1)
        self.assertEqual(
            [frame["sequence"] for frame in first_all_frames],
            sorted(frame["sequence"] for frame in first_all_frames),
        )
        self.assertEqual(
            [frame["sequence"] for frame in second_all_frames],
            sorted(frame["sequence"] for frame in second_all_frames),
        )

    async def test_compatibility_boundaries_reject_missing_authenticated_context(self):
        coding = CodingRequest(
            role="backend-coder",
            workspace=str(self.root),
            user_id="forged",
            task="x",
            request_key="direct-coding",
        )
        mapper = MapperRequest(
            request_key="direct-read",
            task="x",
            workspace=str(self.root),
            user_id="forged",
            model="free",
            connection={},
            parent_chat_id="p",
        )
        with self.assertRaises(TypeError):
            await run_coding_specialist(
                coding, model="free", connection={}, parent_chat_id="p", store=self.store
            )
        with self.assertRaises(TypeError):
            await run_browser_debugger(
                coding, model="free", connection={}, parent_chat_id="p", store=self.store
            )
        with self.assertRaises(TypeError):
            await run_read_only_specialist(mapper, "mapper", store=self.store)


if __name__ == "__main__":
    unittest.main()