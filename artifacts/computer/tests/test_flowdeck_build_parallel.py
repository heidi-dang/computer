import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from cptr.flowdeck.build import create_build_request
from cptr.flowdeck.build_agent import BuildAgentRequest
from cptr.flowdeck.build_parallel import run_parallel_build_mutations
from cptr.flowdeck.durable import DurableFlowDeck, RunStatus, StepStatus
from cptr.models import Base
from cptr.models.flowdeck import FlowDeckRun
from cptr.models.workspaces import Workspace


class ParallelBuildAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        subprocess.check_call(("git", "-C", str(self.root), "init", "-q"))
        subprocess.check_call(("git", "-C", str(self.root), "config", "user.name", "test"))
        subprocess.check_call(
            ("git", "-C", str(self.root), "config", "user.email", "test@example.invalid")
        )
        (self.root / "README.md").write_text("base\n")
        subprocess.check_call(("git", "-C", str(self.root), "add", "README.md"))
        subprocess.check_call(("git", "-C", str(self.root), "commit", "-qm", "base"))

        self.db = tempfile.NamedTemporaryFile(delete=False)
        self.db.close()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db.name}")
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
            request_key="parallel-parent",
            owner="owner",
            workspace=str(self.root),
            step_name="heidi-coordinator",
        )
        await self.store.start_run(self.parent.id)

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db.name)
        self.temp.cleanup()

    def auth(self):
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

    async def test_parallel_mutations_integrate_and_clean_up(self):
        calls = []

        async def fake_dispatch(_request, dispatch, *, store):
            calls.append(dispatch)
            child, _ = await store.create_run(
                request_key=dispatch.request_key,
                owner="owner",
                workspace=dispatch.execution_workspace,
                step_name=dispatch.role,
            )
            await store.start_run(child.id)
            step = await store.get_step(child.id)
            await store.start_step(step.id)
            operation, _ = await store.record_intent(
                run_id=child.id,
                idempotency_key=f"{dispatch.request_key}:native",
                capability="write_files",
                target=dispatch.role,
                reconcile_kind="native_loop",
                step_id=step.id,
            )
            attempt = await store.prepare_attempt(
                operation_id=operation.id,
                owner="owner",
                fencing_epoch=0,
            )
            if dispatch.role != "tester":
                filename = "backend.py" if dispatch.task.find("data model") >= 0 else "frontend.py"
                Path(dispatch.execution_workspace, filename).write_text("print('ok')\n")
            await store.finish_attempt(
                attempt.id,
                owner="owner",
                fencing_epoch=0,
                outcome="succeeded",
                evidence={
                    "source": "runtime",
                    "authoritative": True,
                    "observation": "native_loop_return",
                    "observed_outcome": "succeeded",
                    "attempt_id": attempt.id,
                    "specialist_claim": None,
                },
            )
            await store.finish_step(step.id, status=StepStatus.SUCCEEDED)
            await store.complete_run(child.id, status=RunStatus.SUCCEEDED)
            return "native"

        request = BuildAgentRequest(
            request_key="parallel-parent",
            task="Build a dashboard",
            workspace=str(self.root),
            user_id="owner",
            model="model",
            connection={},
            parent_chat_id="chat",
            parent_message_id="message",
            parent_flowdeck_run_id=self.parent.id,
        )
        with patch(
            "cptr.flowdeck.build_parallel.dispatch_authenticated_specialist",
            side_effect=fake_dispatch,
        ):
            result = await run_parallel_build_mutations(
                request,
                build_request=create_build_request("Build a dashboard"),
                authenticated_request=self.auth(),
                store=self.store,
                coding_role="backend-coder",
            )

        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sum(call.role != "tester" for call in calls), 2)
        self.assertEqual(sum(call.role == "tester" for call in calls), 2)
        self.assertTrue({call.execution_workspace for call in calls})
        self.assertTrue((self.root / "backend.py").exists())
        self.assertTrue((self.root / "frontend.py").exists())
        nodes = await self.store.get_build_nodes(self.parent.id)
        self.assertEqual({node.status for node in nodes}, {"SUCCEEDED"})
        self.assertEqual({node.integration_status for node in nodes}, {"SUCCEEDED"})
        self.assertEqual(
            subprocess.check_output(
                ("git", "-C", str(self.root), "worktree", "list", "--porcelain"),
                text=True,
            ).count("worktree "),
            1,
        )

    async def test_failed_isolated_verification_never_integrates(self):
        calls = []

        async def fake_dispatch(_request, dispatch, *, store):
            calls.append(dispatch)
            child, _ = await store.create_run(
                request_key=dispatch.request_key,
                owner="owner",
                workspace=dispatch.execution_workspace,
                step_name=dispatch.role,
            )
            await store.start_run(child.id)
            step = await store.get_step(child.id)
            await store.start_step(step.id)
            operation, _ = await store.record_intent(
                run_id=child.id,
                idempotency_key=f"{dispatch.request_key}:native",
                capability=(
                    "execute_command"
                    if dispatch.role == "tester"
                    else "write_files"
                ),
                target=dispatch.role,
                reconcile_kind="native_loop",
                step_id=step.id,
            )
            attempt = await store.prepare_attempt(
                operation_id=operation.id,
                owner="owner",
                fencing_epoch=0,
            )
            if dispatch.role != "tester":
                filename = (
                    "backend.py"
                    if "data model" in dispatch.task
                    else "frontend.py"
                )
                Path(dispatch.execution_workspace, filename).write_text("failed\n")
            outcome = not (
                dispatch.role == "tester" and dispatch.check == "tests"
            )
            await store.finish_attempt(
                attempt.id,
                owner="owner",
                fencing_epoch=0,
                outcome="succeeded" if outcome else "failed",
                evidence={
                    "source": "runtime",
                    "authoritative": True,
                    "observation": "native_loop_return",
                    "observed_outcome": "succeeded" if outcome else "failed",
                    "attempt_id": attempt.id,
                    "specialist_claim": None,
                },
            )
            await store.finish_step(
                step.id,
                status=(
                    StepStatus.SUCCEEDED
                    if outcome
                    else StepStatus.FAILED
                ),
            )
            await store.complete_run(
                child.id,
                status=RunStatus.SUCCEEDED if outcome else RunStatus.FAILED,
            )
            return "native"

        request = BuildAgentRequest(
            request_key="verification-failure-parent",
            task="Build a dashboard",
            workspace=str(self.root),
            user_id="owner",
            model="global-model",
            connection={},
            parent_chat_id="chat",
            parent_message_id="message",
            parent_flowdeck_run_id=self.parent.id,
        )
        with patch(
            "cptr.flowdeck.build_parallel.dispatch_authenticated_specialist",
            side_effect=fake_dispatch,
        ):
            result = await run_parallel_build_mutations(
                request,
                build_request=create_build_request("Build a dashboard"),
                authenticated_request=self.auth(),
                store=self.store,
                coding_role="backend-coder",
            )

        self.assertEqual(result["status"], "manual_review_required", result)
        self.assertFalse((self.root / "backend.py").exists())
        events = await self.store.list_events(self.parent.id)
        self.assertFalse(
            any(event.kind == "BUILD_NODE_INTEGRATED" for event in events)
        )
        failed = [
            event
            for event in events
            if event.kind == "BUILD_NODE_FINISHED"
            and (event.payload or {}).get("status") == "FAILED"
        ]
        self.assertEqual(len(failed), 2)
        self.assertTrue(
            all(
                (event.payload or {}).get("evidence", {}).get(
                    "verification_failed_at"
                )
                for event in failed
            )
        )
        self.assertEqual(
            sum(call.role == "tester" and call.check == "tests" for call in calls),
            2,
        )

    async def test_authenticated_gateway_keeps_real_tester_for_parallel_worktrees(self):
        """Qualification fixture: only the model callback is deterministic.

        The authenticated gateway, real worktree validation, real structured
        tester subprocess, durable tester attempts, and integration remain
        production code paths.
        """
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_smoke.py").write_text(
            "import unittest\n\nclass Smoke(unittest.TestCase):\n"
            "    def test_ok(self): self.assertTrue(True)\n"
        )
        frontend = self.root / "cptr" / "frontend"
        frontend.mkdir(parents=True)
        (frontend / "package.json").write_text(
            '{"scripts":{"build":"node -e \\"process.exit(0)\\""}}\n'
        )
        subprocess.check_call(("git", "-C", str(self.root), "add", "."))
        subprocess.check_call(
            ("git", "-C", str(self.root), "commit", "-qm", "qualification baseline")
        )

        async def deterministic_native(coding_request, *, model, connection,
                                       parent_chat_id, parent_flowdeck_run_id,
                                       parent_message_id, store):
            child, _ = await store.create_run(
                request_key=coding_request.request_key,
                owner="owner",
                workspace=coding_request.workspace,
                step_name=coding_request.role,
            )
            await store.start_run(child.id)
            step = await store.get_step(child.id)
            await store.start_step(step.id)
            operation, _ = await store.record_intent(
                run_id=child.id,
                idempotency_key=f"{coding_request.request_key}:native",
                capability="write_files",
                target=coding_request.role,
                reconcile_kind="native_loop",
                step_id=step.id,
            )
            attempt = await store.prepare_attempt(
                operation_id=operation.id, owner="owner", fencing_epoch=0
            )
            filename = (
                "backend.py"
                if "data model" in coding_request.task
                else "frontend.py"
            )
            Path(coding_request.workspace, filename).write_text("print('ok')\n")
            await store.finish_attempt(
                attempt.id,
                owner="owner",
                fencing_epoch=0,
                outcome="succeeded",
                evidence={
                    "source": "runtime",
                    "authoritative": True,
                    "observation": "native_loop_return",
                    "observed_outcome": "succeeded",
                    "attempt_id": attempt.id,
                    "specialist_claim": None,
                },
            )
            await store.finish_step(step.id, status=StepStatus.SUCCEEDED)
            await store.complete_run(child.id, status=RunStatus.SUCCEEDED)
            return "native"

        request = BuildAgentRequest(
            request_key="real-tester-parent",
            task="Build a dashboard",
            workspace=str(self.root),
            user_id="owner",
            model="verified-model",
            connection={},
            parent_chat_id="chat",
            parent_message_id="message",
            parent_flowdeck_run_id=self.parent.id,
        )
        with patch(
            "cptr.flowdeck.authenticated_gateway._native_run_coding_specialist",
            side_effect=deterministic_native,
        ):
            result = await run_parallel_build_mutations(
                request,
                build_request=create_build_request("Build a dashboard"),
                authenticated_request=self.auth(),
                store=self.store,
                coding_role="backend-coder",
            )

        self.assertEqual(result["status"], "succeeded", result)
        nodes = await self.store.get_build_nodes(self.parent.id)
        self.assertEqual({node.status for node in nodes}, {"SUCCEEDED"})
        async with self.store.session_factory() as session:
            tester_runs = list(
                (
                    await session.scalars(
                        select(FlowDeckRun).where(
                            FlowDeckRun.request_key.like(
                                "real-tester-parent:build:node:%:verify:%"
                            )
                        )
                    )
                ).all()
            )
        self.assertEqual(len(tester_runs), 2)
        self.assertTrue(all(run.status == RunStatus.SUCCEEDED.value for run in tester_runs))
        self.assertEqual(
            subprocess.check_output(
                ("git", "-C", str(self.root), "worktree", "list", "--porcelain"),
                text=True,
            ).count("worktree "),
            1,
        )


if __name__ == "__main__":
    unittest.main()

