import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from cptr.flowdeck.build import create_build_request
from cptr.flowdeck.build_agent import (
    BuildAgentPolicyError,
    BuildAgentRequest,
    run_build_agent,
    validate_build_agent_request,
)
from cptr.flowdeck.durable import DurableFlowDeck, RunStatus, StepStatus
from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import FlowDeckMode
from cptr.models import Base
from cptr.models.workspaces import Workspace


class BuildAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.db_file = tempfile.NamedTemporaryFile(delete=False)
        self.db_file.close()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_file.name}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False), clock=lambda: 1000
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
        self.parent, _ = await self.store.create_run(
            request_key="build-parent",
            owner="owner",
            workspace=str(self.root),
            step_name="heidi-coordinator",
        )
        await self.store.start_run(self.parent.id)
        parent_step = await self.store.get_step(self.parent.id)
        await self.store.start_step(parent_step.id)
        self.request = BuildAgentRequest(
            request_key="build-parent",
            task="Build a dashboard",
            workspace=str(self.root),
            user_id="owner",
            model="model",
            connection={},
            parent_chat_id="chat",
            parent_message_id="message",
            parent_flowdeck_run_id=self.parent.id,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_file.name)
        self.temp.cleanup()

    def _auth(self):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/internal",
                "headers": [],
                "client": ("127.0.0.1", 0),
                "server": ("internal", 0),
                "scheme": "http",
                "state": {},
            }
        )
        request.state.auth = type("Auth", (), {"user_id": "owner"})()
        return request

    async def test_build_agent_runs_native_mutation_and_every_contract_check(self):
        calls = []
        steering_calls = 0

        async def steering_checkpoint():
            nonlocal steering_calls
            steering_calls += 1
            return ["keep the existing project conventions"]

        async def native_child(_request, dispatch, *, store):
            calls.append(dispatch)
            child, _ = await store.create_run(
                request_key=dispatch.request_key,
                owner="owner",
                workspace=str(self.root),
                step_name=dispatch.role,
            )
            await store.start_run(child.id)
            step = await store.get_step(child.id)
            await store.start_step(step.id)
            operation, _ = await store.record_intent(
                run_id=child.id,
                idempotency_key=f"{dispatch.request_key}:native",
                capability="write_files"
                if dispatch.role in {"backend-coder", "frontend-coder"}
                else "execute_command",
                target=dispatch.role,
                reconcile_kind="native_loop",
                step_id=step.id,
            )
            attempt = await store.prepare_attempt(
                operation_id=operation.id, owner="owner", fencing_epoch=0
            )
            await store.finish_attempt(
                attempt.id,
                owner="owner",
                fencing_epoch=0,
                outcome="succeeded",
                evidence={
                    "source": "verifier",
                    "authoritative": True,
                    "observation": "verifier_check",
                    "observed_outcome": "succeeded",
                    "attempt_id": attempt.id,
                },
            )
            await store.finish_step(step.id, status=StepStatus.SUCCEEDED)
            await store.complete_run(child.id, status=RunStatus.SUCCEEDED)
            return {} if dispatch.role == "tester" else "native mutation"

        env = {
            "CPTR_FLOWDECK_ENABLED": "true",
            "CPTR_FLOWDECK_COORDINATOR_ENABLED": "true",
            "CPTR_FLOWDECK_MODE": "controlled",
            "CPTR_FLOWDECK_GOVERNANCE": "strict",
            "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
        }
        with patch.dict(os.environ, env, clear=False), patch(
            "cptr.flowdeck.build_agent.dispatch_authenticated_specialist",
            side_effect=native_child,
        ):
            result = await run_build_agent(
                self.request,
                build_request=create_build_request("Build a dashboard"),
                authenticated_request=self._auth(),
                store=self.store,
                steering_checkpoint=steering_checkpoint,
            )

        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(calls), 7)
        self.assertEqual(steering_calls, 7)
        self.assertEqual(calls[0].role, "backend-coder")
        self.assertIn("existing project conventions", calls[0].task)
        self.assertEqual(
            [call.check for call in calls[1:]],
            ["tests", "tests", "tests", "build", "build", "typecheck"],
        )
        self.assertEqual(
            set(result["evidence"]),
            set(create_build_request("Build a dashboard").completion.required_checks),
        )

    def test_mutation_must_be_explicitly_qualified(self):
        request = self.request
        contract = create_build_request("Build a dashboard")
        config = FlowDeckConfig(
            enabled=True,
            coordinator_enabled=True,
            mode=FlowDeckMode.CONTROLLED,
            governance="strict",
            mutating_agents=False,
        )
        with self.assertRaises(BuildAgentPolicyError):
            validate_build_agent_request(request, contract, config)


if __name__ == "__main__":
    unittest.main()