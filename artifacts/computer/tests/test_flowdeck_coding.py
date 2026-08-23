import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from cptr.flowdeck.budgets import BudgetExceeded, RunBudget
from cptr.flowdeck.coding import (
    CODING_SPECIALIST_ROLES,
    CodingPolicyError,
    CodingRequest,
    _native_run_browser_debugger,
    _native_run_coding_specialist,
    browser_tool_guard,
    coding_tool_guard,
    resolve_authorized_workspace,
    validate_browser_request,
    validate_coding_request,
)
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import FlowDeckMode
from cptr.flowdeck.durable import DurableFlowDeck
from cptr.models.base import Base
from cptr.models.flowdeck import FlowDeckLogicalOperation, FlowDeckPhysicalAttempt
from cptr.models.workspaces import Workspace
from cptr.utils.config import AuthResult
from cptr.utils.tools import execute_tool


class CodingContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("pass\n")
        self.request = CodingRequest(
            role="backend-coder",
            workspace=self.workspace.name,
            user_id="user-1",
            task="make a bounded change",
            request_key="coding-request",
        )
        self.config = FlowDeckConfig(
            enabled=True,
            mode=FlowDeckMode.CONTROLLED,
            governance="strict",
            mutating_agents=True,
        )

    def tearDown(self):
        self.workspace.cleanup()

    def test_only_one_role_is_eligible_and_policy_is_explicit(self):
        validate_coding_request(self.request, self.config)
        frontend_request = self.request.__class__(
            **{**self.request.__dict__, "role": "frontend-coder"}
        )
        frontend_config = self.config.__class__(
            **{**self.config.__dict__, "coding_role": "frontend-coder"}
        )
        validate_coding_request(frontend_request, frontend_config)
        for role in CODING_SPECIALIST_ROLES:
            if role in {"backend-coder", "frontend-coder"}:
                continue
            with self.assertRaises(CodingPolicyError):
                validate_coding_request(
                    self.request.__class__(**{**self.request.__dict__, "role": role}),
                    self.config,
                )
        with self.assertRaises(CodingPolicyError):
            validate_coding_request(
                self.request,
                self.config.__class__(**{**self.config.__dict__, "mutating_agents": False}),
            )

    def test_runtime_guard_enforces_role_tools_and_owned_paths(self):
        context = {
            "workspace": self.workspace.name,
            "specialist_role": "backend-coder",
        }
        self.assertTrue(coding_tool_guard("edit_file", {"path": "src/main.py"}, context))
        self.assertFalse(coding_tool_guard("run_command", {"command": "rm -rf /"}, context))
        self.assertFalse(coding_tool_guard("edit_file", {"path": "../outside"}, context))
        self.assertFalse(coding_tool_guard("write_file", {"path": ".git/config"}, context))
        self.assertFalse(coding_tool_guard("write_file", {"path": ".env"}, context))
        outside = self.root / "outside.txt"
        outside_dir = tempfile.TemporaryDirectory()
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name) / "outside.txt"
        outside.write_text("not owned\n")
        (self.root / "src" / "link").symlink_to(outside)
        self.assertFalse(coding_tool_guard("write_file", {"path": "src/link"}, context))
        self.assertFalse(
            coding_tool_guard(
                "browser_click",
                {"ref": "button"},
                {**context, "specialist_role": "backend-coder"},
            )
        )

    def test_browser_debugger_is_local_preview_read_only(self):
        request = self.request.__class__(
            **{**self.request.__dict__, "role": "browser-debugger"}
        )
        config = self.config.__class__(
            **{
                **self.config.__dict__,
                "coding_role": "browser-debugger",
                "mutating_agents": False,
            }
        )
        validate_browser_request(request, config)
        context = {"workspace": self.workspace.name}
        self.assertTrue(browser_tool_guard("browser_snapshot", {}, context))
        self.assertTrue(
            browser_tool_guard(
                "browser_navigate",
                {"url": "http://127.0.0.1:8080/"},
                context,
            )
        )
        self.assertFalse(
            browser_tool_guard("browser_navigate", {"url": "https://example.com"}, context)
        )
        self.assertFalse(browser_tool_guard("browser_click", {"ref": "x"}, context))

    async def test_native_mutation_stays_gated_until_per_write_hooks_exist(self):
        with self.assertRaises(CodingPolicyError):
            await _native_run_coding_specialist(
                self.request,
                model="model",
                connection={},
                parent_chat_id="parent",
                store=None,
            )

    def test_budgets_fail_closed(self):
        budget = RunBudget(
            max_steps=1,
            max_attempts=1,
            max_delegations=1,
            max_tool_calls=1,
            max_model_turns=1,
            max_wall_seconds=1,
        )
        budget.consume_step()
        with self.assertRaises(BudgetExceeded):
            budget.consume_step()
        budget.consume_attempt()
        budget.consume_delegation()
        budget.consume_tool_call()
        budget.consume_model_turn()
        with self.assertRaises(BudgetExceeded):
            budget.validate_wall_time(2)


class CodingExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.root = Path(self.workspace.name)
        db_fd, self.db_path = tempfile.mkstemp()
        os.close(db_fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False),
            clock=lambda: 1000,
        )
        async with self.store.session_factory() as session:
            session.add(
                Workspace(
                    user_id="user-1",
                    path=str(self.root.resolve()),
                    name="qualification",
                    data={},
                    created_at=1000,
                )
            )
            await session.commit()
        self.request = CodingRequest(
            role="backend-coder",
            workspace=self.workspace.name,
            user_id="user-1",
            task="inspect only",
            request_key="coding-native-request",
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_path)
        self.workspace.cleanup()

    async def test_backend_coder_reuses_native_loop_with_mutation_hooks(self):
        fake_chat = type("Chat", (), {"id": "coding-chat"})()
        fake_message = type("Message", (), {"id": "coding-message"})()
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                    "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                },
                clear=False,
            ),
            patch(
                "cptr.utils.tools._create_subagent_chat",
                new=AsyncMock(return_value=(fake_chat, None, fake_message)),
            ),
            patch(
                "cptr.utils.tools._run_existing_subagent_chat",
                new=AsyncMock(return_value="native result"),
            ) as run_chat,
        ):
            result = await _native_run_coding_specialist(
                self.request,
                model="model",
                connection={"provider": "test"},
                parent_chat_id="parent",
                store=self.store,
            )
        self.assertEqual(result, "native result")
        self.assertIsNotNone(run_chat.await_args.kwargs["before_mutation"])
        self.assertIsNotNone(run_chat.await_args.kwargs["after_mutation"])
        self.assertEqual(run_chat.await_args.kwargs["specialist_role"], "backend-coder")

    async def test_workspace_ownership_resolver_fails_closed_for_wrong_stale_and_ambiguous_scope(self):
        resolver = lambda user, path: resolve_authorized_workspace(
            session_factory=self.store.session_factory,
            user_id=user,
            workspace=path,
        )
        with self.assertRaises(CodingPolicyError):
            await resolver("other-user", str(self.root))
        with self.assertRaises(CodingPolicyError):
            await resolver("user-1", str(self.root / "forged"))
        async with self.store.session_factory() as session:
            await session.execute(
                __import__("sqlalchemy").delete(Workspace).where(Workspace.user_id == "user-1")
            )
            await session.commit()
        with self.assertRaises(CodingPolicyError):
            await resolver("user-1", str(self.root))

    async def test_runtime_identity_mismatch_is_denied_before_mutation(self):
        from cptr.flowdeck.coding import _runtime_workspace_matches

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
        request.state.auth = AuthResult(user_id="other-user", role="user")
        self.assertFalse(
            _runtime_workspace_matches(
                context={"request": request, "workspace": str(self.root)},
                user_id="user-1",
                root=self.root,
            )
        )

    async def test_each_coding_role_executes_real_structured_mutation_with_durable_evidence(self):
        for role in ("backend-coder", "frontend-coder"):
            path = self.root / f"{role}.txt"
            path.write_text("before\n")
            request = self.request.__class__(
                **{
                    **self.request.__dict__,
                    "role": role,
                    "request_key": f"real-{role}",
                }
            )
            fake_chat = type("Chat", (), {"id": f"{role}-chat"})()
            fake_message = type("Message", (), {"id": f"{role}-message"})()

            async def native_loop(*args, role=role, path=path, **kwargs):
                auth_request = Request(
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
                auth_request.state.auth = AuthResult(user_id="user-1", role="user")
                context = {
                    "workspace": self.workspace.name,
                    "specialist_role": role,
                    "allowed_tool_names": kwargs["allowed_tool_names"],
                    "tool_guard": kwargs["tool_guard"],
                    "before_mutation": kwargs["before_mutation"],
                    "after_mutation": kwargs["after_mutation"],
                    "mutation_tool_names": frozenset(
                        {"edit_file", "multi_edit_file", "write_file"}
                    ),
                    "request": auth_request,
                }
                return await execute_tool(
                    "edit_file",
                    {
                        "path": path.name,
                        "target": "before\n",
                        "replacement": "after\n",
                    },
                    context,
                )

            with (
                patch.dict(
                    os.environ,
                    {
                        "CPTR_FLOWDECK_ENABLED": "true",
                        "CPTR_FLOWDECK_MODE": "controlled",
                        "CPTR_FLOWDECK_GOVERNANCE": "strict",
                        "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                        "CPTR_FLOWDECK_CODING_ROLE": role,
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
                result = await _native_run_coding_specialist(
                    request,
                    model="model",
                    connection={"provider": "test"},
                    parent_chat_id="parent",
                    store=self.store,
                )

            self.assertIn("Edited", result)
            self.assertEqual(path.read_text(), "after\n")
            async with self.store.session_factory() as session:
                operations = list(
                    (
                        await session.scalars(
                            select(FlowDeckLogicalOperation).where(
                                FlowDeckLogicalOperation.idempotency_key.like(
                                    f"real-{role}:%"
                                )
                            )
                        )
                    ).all()
                )
                attempts = list(
                    (
                        await session.scalars(
                            select(FlowDeckPhysicalAttempt).where(
                                FlowDeckPhysicalAttempt.operation_id.in_(
                                    [operation.id for operation in operations]
                                )
                            )
                        )
                    ).all()
                )
            self.assertEqual(len(operations), 2)
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(attempt.status == "SUCCEEDED" for attempt in attempts))
            self.assertTrue(
                all(
                    operation.authoritative_evidence["authoritative"]
                    for operation in operations
                )
            )

    async def test_unverified_coding_mutation_becomes_manual_review(self):
        fake_chat = type("Chat", (), {"id": "pending-chat"})()
        fake_message = type("Message", (), {"id": "pending-message"})()
        path = self.root / "pending.txt"
        path.write_text("before\n")

        async def interrupted_loop(*args, **kwargs):
            auth_request = Request(
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
            auth_request.state.auth = AuthResult(user_id="user-1", role="user")
            context = {
                "workspace": self.workspace.name,
                "specialist_role": "backend-coder",
                "call_id": "pending-call",
                "request": auth_request,
            }
            self.assertTrue(
                await kwargs["before_mutation"](
                    "write_file", {"path": path.name}, context
                )
            )
            return "interrupted before verifier"

        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                    "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                },
                clear=False,
            ),
            patch(
                "cptr.utils.tools._create_subagent_chat",
                new=AsyncMock(return_value=(fake_chat, None, fake_message)),
            ),
            patch(
                "cptr.utils.tools._run_existing_subagent_chat",
                new=interrupted_loop,
            ),
            self.assertRaises(CodingPolicyError),
        ):
            await _native_run_coding_specialist(
                self.request,
                model="model",
                connection={"provider": "test"},
                parent_chat_id="parent",
                store=self.store,
            )

    async def test_coding_direct_dispatch_denies_all_unqualified_capabilities(self):
        context = {
            "workspace": self.workspace.name,
            "specialist_role": "backend-coder",
            "allowed_tool_names": frozenset(
                {"read_file", "search_files", "edit_file", "multi_edit_file", "write_file"}
            ),
            "tool_guard": coding_tool_guard,
        }
        forbidden = {
            "run_command",
            "send_input",
            "kill_task",
            "git_commit",
            "git_push",
            "mcp_tool",
            "browser_click",
            "browser_type",
            "browser_evaluate",
            "deploy",
            "publish",
            "read_secret",
            "install_package",
            "network_write",
        }
        for name in forbidden:
            result = await execute_tool(name, {}, context)
            self.assertIn("tool denied by execution policy", result, name)

    async def test_browser_debugger_reuses_native_loop_without_mutation_hooks(self):
        fake_chat = type("Chat", (), {"id": "browser-chat"})()
        fake_message = type("Message", (), {"id": "browser-message"})()
        request = self.request.__class__(
            **{**self.request.__dict__, "role": "browser-debugger"}
        )
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                    "CPTR_FLOWDECK_MUTATING_AGENTS": "false",
                    "CPTR_FLOWDECK_CODING_ROLE": "browser-debugger",
                },
                clear=False,
            ),
            patch(
                "cptr.utils.tools._create_subagent_chat",
                new=AsyncMock(return_value=(fake_chat, None, fake_message)),
            ),
            patch(
                "cptr.utils.tools._run_existing_subagent_chat",
                new=AsyncMock(return_value="browser result"),
            ) as run_chat,
        ):
                result = await _native_run_browser_debugger(
                request,
                model="model",
                connection={"provider": "test"},
                parent_chat_id="parent",
                store=self.store,
            )
        self.assertEqual(result, "browser result")
        self.assertEqual(
            run_chat.await_args.kwargs["allowed_tool_names"],
            frozenset(
                {
                    "read_file",
                    "search_files",
                    "browser_navigate",
                    "browser_snapshot",
                    "browser_screenshot",
                }
            ),
        )
        self.assertNotIn("before_mutation", run_chat.await_args.kwargs)
