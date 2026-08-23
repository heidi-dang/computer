import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.flowdeck.budgets import BudgetExceeded, RunBudget
from cptr.flowdeck.coding import (
    CODING_SPECIALIST_ROLES,
    CodingPolicyError,
    CodingRequest,
    coding_tool_guard,
    run_coding_specialist,
    validate_coding_request,
)
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import FlowDeckMode
from cptr.flowdeck.durable import DurableFlowDeck
from cptr.models.base import Base


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
        for role in CODING_SPECIALIST_ROLES:
            if role == "backend-coder":
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

    async def test_native_mutation_stays_gated_until_per_write_hooks_exist(self):
        with self.assertRaises(CodingPolicyError):
            await run_coding_specialist(
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
        db_fd, self.db_path = tempfile.mkstemp()
        os.close(db_fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False),
            clock=lambda: 1000,
        )
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
            result = await run_coding_specialist(
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
