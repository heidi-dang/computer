import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, FactoryApproval
from cptr.routers.factory import (
    FactoryApprovalRequest,
    FactoryMessageRequest,
    FactoryRunStartRequest,
    approve_factory_run,
    factory_router,
    start_factory_run,
)
from cptr.services.factory_control import (
    FactoryControlConflict,
    FactoryControlNotFound,
    FactoryControlService,
)
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_gates import EvidenceAuthority
from cptr.services.factory_store import SqlFactoryStore
from cptr.services.factory_workers import WorkerQuiescenceResult


class _Workers:
    def __init__(self, result: WorkerQuiescenceResult):
        self.result = result
        self.calls = []

    async def cancel_run(self, run, *, timeout_ms: int):
        self.calls.append((run.id, timeout_ms))
        return self.result


class FactoryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)
        self.workers = _Workers(
            WorkerQuiescenceResult(
                quiescent=True,
                failed_command_ids=(),
                unresolved_assignment_ids=(),
            )
        )
        self.service = FactoryControlService(
            store=self.store,
            session_factory=self.sessions,
            worker_controller=self.workers,
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _start(self, *, user_id="user-1", workspace_id="workspace-1", key="start-1"):
        return await self.service.start(
            user_id=user_id,
            workspace_id=workspace_id,
            mission="implement the requested factory mission",
            acceptance_criteria=("all required machine gates pass",),
            policy={"max_cycles": 1},
            budget={"max_repair_attempts_per_signature": 3},
            model_id="configured-model",
            idempotency_key=key,
        )

    async def test_start_is_user_scoped_idempotent_and_preserves_original_immutable_payload(self):
        first = await self._start()
        replay = await self._start()
        self.assertEqual(first.id, replay.id)

        changed_replay = await self.service.start(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="different mission under same key",
            acceptance_criteria=("different criterion",),
            policy={"max_cycles": 99},
            budget={"max_repair_attempts_per_signature": 99},
            model_id="other-model",
            idempotency_key="start-1",
        )
        self.assertEqual(changed_replay.id, first.id)
        self.assertEqual(changed_replay.mission, "implement the requested factory mission")
        self.assertEqual(changed_replay.acceptance_criteria, ["all required machine gates pass"])
        self.assertEqual(changed_replay.model_id, "configured-model")

        other_user = await self._start(user_id="user-2", key="start-1")
        self.assertNotEqual(first.id, other_user.id)

    async def test_status_events_and_evidence_are_owner_scoped_cursor_paginated_and_bounded(self):
        run = await self._start()
        for index in range(5):
            await self.service.message(
                user_id="user-1",
                run_id=run.id,
                content=f"message-{index}",
                idempotency_key=f"message-{index}",
            )
        cycle = await self.store.get_cycle(run.current_cycle_id) if run.current_cycle_id else None
        if cycle is None:
            cycle = await self.store.create_cycle(
                run.id,
                base_revision=None,
                base_fingerprint=None,
                idempotency_key="api-cycle",
            )
        for index in range(4):
            await self.store.append_evidence(
                run_id=run.id,
                cycle_id=cycle.id,
                gate_id=None,
                kind="api-test",
                source="machine-test",
                authority=EvidenceAuthority.MACHINE,
                revision=None,
                fingerprint=None,
                payload={"index": index, "blob": "x" * 128},
                idempotency_key=f"api-evidence-{index}",
            )

        status = await self.service.status(user_id="user-1", run_id=run.id)
        self.assertEqual(status["run_id"], run.id)
        self.assertNotIn("lease_token", status)
        self.assertNotIn("config_fingerprint", status)

        with self.assertRaises(FactoryControlNotFound):
            await self.service.status(user_id="user-2", run_id=run.id)

        page1 = await self.service.events(user_id="user-1", run_id=run.id, cursor=None, limit=2)
        self.assertLessEqual(len(page1["events"]), 2)
        self.assertTrue(page1["next_cursor"])
        page2 = await self.service.events(
            user_id="user-1",
            run_id=run.id,
            cursor=page1["next_cursor"],
            limit=2,
        )
        self.assertTrue(
            set(item["sequence"] for item in page1["events"]).isdisjoint(
                item["sequence"] for item in page2["events"]
            )
        )

        evidence1 = await self.service.evidence(
            user_id="user-1", run_id=run.id, cursor=None, limit=2
        )
        self.assertLessEqual(len(evidence1["evidence"]), 2)
        self.assertTrue(evidence1["next_cursor"])
        evidence2 = await self.service.evidence(
            user_id="user-1",
            run_id=run.id,
            cursor=evidence1["next_cursor"],
            limit=2,
        )
        self.assertTrue(
            set(item["evidence_id"] for item in evidence1["evidence"]).isdisjoint(
                item["evidence_id"] for item in evidence2["evidence"]
            )
        )
        self.assertLessEqual(evidence1["bytes_returned"], evidence1["max_bytes"])

    async def test_pause_resume_is_state_authoritative_and_resume_cannot_bypass_approval(self):
        run = await self._start()
        paused = await self.service.pause(
            user_id="user-1", run_id=run.id, idempotency_key="pause-1"
        )
        self.assertEqual(paused.state, FactoryState.PAUSED.value)
        resumed = await self.service.resume(
            user_id="user-1", run_id=run.id, idempotency_key="resume-1"
        )
        self.assertEqual(resumed.state, FactoryState.MISSION.value)

        await self.store.transition(
            run.id,
            to_state=FactoryState.APPROVAL_REQUIRED,
            actor=FactoryActor.SYSTEM,
            reason="test approval boundary",
            idempotency_key="approval-required",
        )
        with self.assertRaises(FactoryControlConflict) as caught:
            await self.service.resume(
                user_id="user-1", run_id=run.id, idempotency_key="resume-bypass"
            )
        self.assertEqual(caught.exception.code, "FACTORY_APPROVAL_REQUIRED")
        current = await self.store.get_run(run.id)
        self.assertEqual(current.state, FactoryState.APPROVAL_REQUIRED.value)

    async def test_approval_is_exact_run_bound_replay_safe_and_releases_only_approved_envelope(
        self,
    ):
        run = await self._start()
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="base",
            base_fingerprint="base-fp",
            idempotency_key="approval-cycle",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.APPROVAL_REQUIRED,
            actor=FactoryActor.SYSTEM,
            reason="push approval required",
            idempotency_key="approval-state",
        )
        approval = await self.service.request_approval(
            run_id=run.id,
            cycle_id=cycle.id,
            kind="git_push",
            revision="commit-sha",
            remote="origin",
            branch="factory-branch",
        )

        approved = await self.service.approve(
            user_id="user-1",
            run_id=run.id,
            approval_id=approval.id,
            approved=True,
            note="approved exact push",
            idempotency_key="approval-decision-1",
        )
        self.assertEqual(approved["approval"]["status"], "APPROVED")
        self.assertEqual(approved["run"]["state"], FactoryState.MISSION.value)

        replay = await self.service.approve(
            user_id="user-1",
            run_id=run.id,
            approval_id=approval.id,
            approved=True,
            note="approved exact push",
            idempotency_key="approval-decision-1",
        )
        self.assertEqual(replay["approval"]["approval_id"], approval.id)

        with self.assertRaises(FactoryControlConflict):
            await self.service.approve(
                user_id="user-1",
                run_id=run.id,
                approval_id=approval.id,
                approved=False,
                note="changed decision",
                idempotency_key="approval-decision-2",
            )

        other = await self._start(user_id="user-1", workspace_id="workspace-2", key="other-run")
        with self.assertRaises(FactoryControlNotFound):
            await self.service.approve(
                user_id="user-1",
                run_id=other.id,
                approval_id=approval.id,
                approved=True,
                note=None,
                idempotency_key="cross-run-replay",
            )

    async def test_approval_decision_is_rejected_until_run_is_waiting_at_approval_boundary(self):
        run = await self._start()
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="base",
            base_fingerprint="base-fp",
            idempotency_key="preapproval-cycle",
        )
        approval = await self.service.request_approval(
            run_id=run.id,
            cycle_id=cycle.id,
            kind="git_push",
            revision="commit-sha",
            remote="origin",
            branch="factory-branch",
        )

        with self.assertRaises(FactoryControlConflict) as caught:
            await self.service.approve(
                user_id="user-1",
                run_id=run.id,
                approval_id=approval.id,
                approved=True,
                note="too early",
                idempotency_key="preapproval-decision",
            )
        self.assertEqual(caught.exception.code, "FACTORY_NOT_APPROVAL_REQUIRED")
        async with self.sessions() as db:
            persisted = await db.get(FactoryApproval, approval.id)
        self.assertEqual(persisted.status, "PENDING")

    async def test_required_approval_denial_blocks_run(self):
        run = await self._start()
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="base",
            base_fingerprint="base-fp",
            idempotency_key="denial-cycle",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.APPROVAL_REQUIRED,
            actor=FactoryActor.SYSTEM,
            reason="push approval required",
            idempotency_key="denial-approval-state",
        )
        approval = await self.service.request_approval(
            run_id=run.id,
            cycle_id=cycle.id,
            kind="git_push",
            revision="commit-sha",
            remote="origin",
            branch="factory-branch",
        )

        denied = await self.service.approve(
            user_id="user-1",
            run_id=run.id,
            approval_id=approval.id,
            approved=False,
            note="deny exact push",
            idempotency_key="denial-decision",
        )

        self.assertEqual(denied["approval"]["status"], "DENIED")
        self.assertEqual(denied["run"]["state"], FactoryState.BLOCKED.value)

    async def test_pending_status_approval_is_the_same_envelope_used_for_push_authorization(self):
        run = await self._start()
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="base",
            base_fingerprint="base-fp",
            idempotency_key="resolver-cycle",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.APPROVAL_REQUIRED,
            actor=FactoryActor.SYSTEM,
            reason="push approval required",
            idempotency_key="resolver-approval-state",
        )
        approval = await self.service.request_approval(
            run_id=run.id,
            cycle_id=cycle.id,
            kind="git_push",
            revision="commit-sha",
            remote="origin",
            branch="factory-branch",
        )

        status = await self.service.status(user_id="user-1", run_id=run.id)
        self.assertEqual(status["pending_approval"]["approval_id"], approval.id)
        pending = await self.service.resolve_push_authorization(
            run_id=run.id,
            cycle_id=cycle.id,
            revision="commit-sha",
            remote="origin",
            branch="factory-branch",
        )
        self.assertIsNone(pending)

        await self.service.approve(
            user_id="user-1",
            run_id=run.id,
            approval_id=approval.id,
            approved=True,
            note="approve exact envelope",
            idempotency_key="resolver-decision",
        )
        authorization = await self.service.resolve_push_authorization(
            run_id=run.id,
            cycle_id=cycle.id,
            revision="commit-sha",
            remote="origin",
            branch="factory-branch",
        )
        self.assertIsNotNone(authorization)
        self.assertEqual(authorization.approval_id, approval.id)
        self.assertEqual(authorization.revision, "commit-sha")
        self.assertEqual(authorization.remote, "origin")
        self.assertEqual(authorization.branch, "factory-branch")

    async def test_stop_transitions_only_after_owned_execution_quiesces(self):
        run = await self._start()
        self.workers.result = WorkerQuiescenceResult(
            quiescent=False,
            failed_command_ids=("cmd-1",),
            unresolved_assignment_ids=(),
        )
        with self.assertRaises(FactoryControlConflict) as caught:
            await self.service.stop(
                user_id="user-1",
                run_id=run.id,
                idempotency_key="stop-1",
                timeout_ms=1_000,
            )
        self.assertEqual(caught.exception.code, "FACTORY_CANCELLATION_NOT_QUIESCENT")
        current = await self.store.get_run(run.id)
        self.assertNotEqual(current.state, FactoryState.CANCELLED.value)

        self.workers.result = WorkerQuiescenceResult(
            quiescent=True,
            failed_command_ids=(),
            unresolved_assignment_ids=(),
        )
        stopped = await self.service.stop(
            user_id="user-1",
            run_id=run.id,
            idempotency_key="stop-2",
            timeout_ms=1_000,
        )
        self.assertEqual(stopped.state, FactoryState.CANCELLED.value)

    async def test_router_exposes_exact_compact_factory_surface_and_is_thin(self):
        methods_paths = {
            (next(iter(route.methods)), route.path)
            for route in factory_router.routes
            if getattr(route, "methods", None)
        }
        expected = {
            ("POST", "/api/control/v1/factory/runs"),
            ("GET", "/api/control/v1/factory/runs/{run_id}"),
            ("GET", "/api/control/v1/factory/runs/{run_id}/events"),
            ("GET", "/api/control/v1/factory/runs/{run_id}/evidence"),
            ("POST", "/api/control/v1/factory/runs/{run_id}/messages"),
            ("POST", "/api/control/v1/factory/runs/{run_id}/pause"),
            ("POST", "/api/control/v1/factory/runs/{run_id}/resume"),
            ("POST", "/api/control/v1/factory/runs/{run_id}/approve"),
            ("POST", "/api/control/v1/factory/runs/{run_id}/stop"),
        }
        self.assertEqual(methods_paths, expected)

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        body = FactoryRunStartRequest(
            workspace_id="workspace-1",
            mission="factory mission",
            acceptance_criteria=["machine verified"],
            idempotency_key="router-start",
        )
        service = SimpleNamespace(
            start=AsyncMock(return_value=SimpleNamespace(id="factory-1", state="MISSION"))
        )
        with (
            patch("cptr.routers.factory._user", new=AsyncMock(return_value="user-1")) as auth,
            patch(
                "cptr.routers.factory._ensure_workspace", new=AsyncMock(return_value=object())
            ) as ensure,
            patch("cptr.routers.factory._service", return_value=service),
        ):
            result = await start_factory_run(request, body)
        self.assertEqual(result["run_id"], "factory-1")
        auth.assert_awaited_once_with(request, "autonomous:run")
        ensure.assert_awaited_once_with("user-1", "workspace-1")
        service.start.assert_awaited_once()

        approval_body = FactoryApprovalRequest(
            approval_id="approval-1",
            approved=True,
            idempotency_key="approval-router",
        )
        service.approve = AsyncMock(
            return_value={"approval": {"approval_id": "approval-1"}, "run": {"state": "PUSHING"}}
        )
        with (
            patch("cptr.routers.factory._user", new=AsyncMock(return_value="user-1")),
            patch("cptr.routers.factory._service", return_value=service),
        ):
            result = await approve_factory_run(request, "factory-1", approval_body)
        self.assertEqual(result["approval"]["approval_id"], "approval-1")

    async def test_router_maps_owner_safe_service_errors_without_leaking_other_run_details(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        body = FactoryMessageRequest(content="hello", idempotency_key="message-1")
        # Ensure public error behavior stays generic for owner misses.
        with patch("cptr.routers.factory._user", new=AsyncMock(return_value="user-1")):
            from cptr.routers.factory import message_factory_run

            with patch("cptr.routers.factory._service") as service_factory:
                service_factory.return_value.message = AsyncMock(
                    side_effect=FactoryControlNotFound("factory run not found")
                )
                with self.assertRaises(HTTPException) as caught:
                    await message_factory_run(request, "foreign-run", body)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.detail, "factory run not found")


if __name__ == "__main__":
    unittest.main()
