import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import (
    Base,
    FactoryCapabilityOutcome,
    FactoryCapabilityRecord,
    FactoryCiRun,
    FactoryCommitIntent,
    FactoryMetricProjection,
    FactoryReasoningCall,
    FactoryWorkerAssignment,
    Workspace,
)
from cptr.routers import mcp as mcp_router
from cptr.services.factory_domain import FactoryActor, FactoryState
from cptr.services.factory_gates import EvidenceAuthority
from cptr.services.factory_observability import FactoryObservabilityService
from cptr.services.factory_store import SqlFactoryStore


class FactoryObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)
        self.service = FactoryObservabilityService(session_factory=self.sessions)

        async with self.sessions() as db:
            db.add(
                Workspace(
                    id="workspace-1",
                    user_id="user-1",
                    path="/workspace-1",
                    name="Factory Repo",
                    data={},
                    created_at=1,
                    updated_at=1,
                )
            )
            db.add(
                Workspace(
                    id="workspace-2",
                    user_id="user-2",
                    path="/workspace-2",
                    name="Other Repo",
                    data={},
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _run(self, user_id="user-1", workspace_id="workspace-1", key="run-1"):
        return await self.store.create_run(
            user_id=user_id,
            workspace_id=workspace_id,
            mission="Build and verify the durable factory dashboard",
            acceptance_criteria=("all required gates pass", "machine evidence is retained"),
            policy={"network": False},
            budget={"max_cycles": 2},
            model_id="configured-model",
            idempotency_key=key,
        )

    async def test_snapshot_is_owner_scoped_and_includes_durable_execution_detail(self):
        run = await self._run()
        other = await self._run(user_id="user-2", workspace_id="workspace-2", key="other-run")
        cycle = await self.store.create_cycle(
            run.id,
            base_revision="base-sha",
            base_fingerprint="base-fp",
            idempotency_key="cycle-1",
        )
        evidence = await self.store.append_evidence(
            run_id=run.id,
            cycle_id=cycle.id,
            gate_id="unit-tests",
            kind="verification_result",
            source="python_pytest",
            authority=EvidenceAuthority.MACHINE,
            revision="target-sha",
            fingerprint="target-fp",
            payload={"passed": 12},
            idempotency_key="evidence-1",
        )
        await self.store.record_gate(
            run_id=run.id,
            cycle_id=cycle.id,
            gate_id="unit-tests",
            category="unit",
            required=True,
            applicable=True,
            status="PASS",
            evidence_ids=[evidence.id],
            evaluated_revision="target-sha",
            evaluated_fingerprint="target-fp",
            reason="machine tests passed",
            attempt=1,
            idempotency_key="gate-1",
        )
        await self.store.append_user_event(
            run_id=run.id,
            event_type="user.message",
            payload={"content": "continue"},
            idempotency_key="message-1",
            cycle_id=cycle.id,
        )

        async with self.sessions() as db:
            db.add(
                FactoryWorkerAssignment(
                    id="fworker-1",
                    run_id=run.id,
                    cycle_id=cycle.id,
                    workspace_id="workspace-1",
                    worker_id="worker-1",
                    owner_key="factory-owner",
                    mode="MUTATION",
                    repo_path=".",
                    scope=["cptr"],
                    branch="factory/ui",
                    base_revision="base-sha",
                    status="ACTIVE",
                    created_at=10,
                    updated_at=10,
                )
            )
            db.add(
                FactoryReasoningCall(
                    id="reason-1",
                    run_id=run.id,
                    cycle_id=cycle.id,
                    role="ARCHITECT",
                    role_ordinal=1,
                    schema_id="architect.v1",
                    provider="openai",
                    model="configured-model",
                    response_id="response-1",
                    input_tokens=120,
                    output_tokens=80,
                    total_tokens=200,
                    runtime_ms=1500,
                    cost_microusd=2500,
                    attempt_count=1,
                    data={"decision": "use persisted evidence"},
                    provider_metadata={},
                    created_at=11,
                )
            )
            db.add(
                FactoryMetricProjection(
                    id="metric-1",
                    run_id=run.id,
                    cycle_id=cycle.id,
                    scope="run",
                    dimension_key="",
                    attempts=1,
                    repair_iterations=0,
                    regressions=0,
                    input_tokens=120,
                    output_tokens=80,
                    runtime_ms=1500,
                    cost_microusd=2500,
                    gate_latency_ms=400,
                    verified_outcome="PASS",
                    updated_at=12,
                )
            )
            capability = FactoryCapabilityRecord(
                id="capability-1",
                stable_id="cptr-direct-coding",
                version="1",
                origin_type="builtin",
                origin_uri="builtin://cptr-direct-coding",
                pinned_version_or_commit="1",
                digest="a" * 64,
                capabilities=["coding"],
                permissions=["workspace:write"],
                network_requirements=[],
                execution_requirements=["cptr-direct-coding"],
                risk_classification="LOW",
                trust_status="APPROVED",
                verification_status="CAPABILITY_TESTED",
                maintenance_metadata={},
                historical_factory_score_ppm=990000,
                created_at=12,
                evaluated_at=12,
            )
            db.add(capability)
            db.add(
                FactoryCapabilityOutcome(
                    id="outcome-1",
                    run_id=run.id,
                    cycle_id=cycle.id,
                    capability_id=capability.id,
                    repository_family="python",
                    task_family="feature",
                    verified_success=True,
                    proof_event_id=(await self.store.list_events(run.id))[-1].id,
                    regression=False,
                    repair_iterations=0,
                    input_tokens=10,
                    output_tokens=5,
                    runtime_ms=100,
                    cost_microusd=50,
                    created_at=13,
                )
            )
            db.add(
                FactoryCommitIntent(
                    id="commit-1",
                    run_id=run.id,
                    cycle_id=cycle.id,
                    repository_key="repo",
                    verified_revision="target-sha",
                    verified_fingerprint="target-fp",
                    diff_digest="b" * 64,
                    changed_paths=["cptr/example.py"],
                    commit_message="feat: example",
                    status="COMMITTED",
                    commit_sha="commit-sha",
                    push_status="PUSHED",
                    push_remote="origin",
                    push_branch="factory/ui",
                    push_approval_id=None,
                    created_at=14,
                    updated_at=14,
                    committed_at=14,
                    pushed_at=14,
                )
            )
            db.add(
                FactoryCiRun(
                    id="ci-1",
                    run_id=run.id,
                    cycle_id=cycle.id,
                    provider="github",
                    repository="heidi-dang/computer",
                    revision="commit-sha",
                    external_run_id="123",
                    check_id="backend",
                    status="COMPLETED",
                    conclusion="SUCCESS",
                    url="https://example.invalid/ci/123",
                    failure_summary=None,
                    diagnosis_required=False,
                    diagnosis_summary=None,
                    created_at=15,
                    updated_at=15,
                    last_observed_at=15,
                    diagnosed_at=None,
                )
            )
            await db.commit()

        snapshot = await self.service.snapshot(user_id="user-1")
        self.assertEqual([item["run_id"] for item in snapshot["runs"]], [run.id])
        self.assertNotIn(other.id, [item["run_id"] for item in snapshot["runs"]])
        selected = snapshot["selected"]
        self.assertEqual(selected["workspace_name"], "Factory Repo")
        self.assertEqual(selected["summary"]["required_gates"], 1)
        self.assertEqual(selected["summary"]["passed_required_gates"], 1)
        self.assertEqual(selected["summary"]["active_workers"], 1)
        self.assertEqual(selected["summary"]["reasoning_calls"], 1)
        self.assertEqual(selected["summary"]["input_tokens"], 120)
        self.assertEqual(selected["gates"][0]["status"], "PASS")
        self.assertEqual(selected["evidence"][0]["authority"], "MACHINE")
        self.assertEqual(selected["workers"][0]["worker_id"], "worker-1")
        self.assertEqual(selected["reasoning"][0]["role"], "ARCHITECT")
        self.assertEqual(selected["capability_outcomes"][0]["stable_id"], "cptr-direct-coding")
        self.assertEqual(selected["commit_intents"][0]["commit_sha"], "commit-sha")
        self.assertEqual(selected["ci_runs"][0]["conclusion"], "SUCCESS")

    async def test_progress_is_server_authoritative_and_tracks_canonical_factory_phase(self):
        run = await self._run()
        await self.store.transition(
            run.id,
            to_state=FactoryState.RECOVERING,
            actor=FactoryActor.SYSTEM,
            reason="mission accepted",
            idempotency_key="progress-recovering",
        )
        await self.store.transition(
            run.id,
            to_state=FactoryState.BASELINING,
            actor=FactoryActor.SYSTEM,
            reason="recovery complete",
            idempotency_key="progress-baselining",
        )
        snapshot = await self.service.snapshot(user_id="user-1", run_id=run.id)
        progress = snapshot["selected"]["progress"]
        self.assertEqual(progress["state"], FactoryState.BASELINING.value)
        self.assertEqual(progress["effective_state"], FactoryState.BASELINING.value)
        self.assertEqual(progress["basis"], "server_state_machine")
        self.assertEqual(progress["phase_index"], 3)
        self.assertEqual(progress["phase_count"], 25)
        self.assertGreater(progress["percent"], 0)
        self.assertLess(progress["percent"], 100)
        self.assertEqual(progress["outcome"], "running")
        durable_events = await self.store.list_events(run.id)
        self.assertEqual(progress["last_event_at_ms"], durable_events[-1].created_at)
        self.assertEqual(progress["state_started_at_ms"], durable_events[-1].created_at)
        self.assertEqual(progress["effective_phase_started_at_ms"], durable_events[-1].created_at)

        await self.store.transition(
            run.id,
            to_state=FactoryState.PAUSED,
            actor=FactoryActor.USER,
            reason="operator pause",
            idempotency_key="progress-paused",
        )
        paused = await self.service.snapshot(user_id="user-1", run_id=run.id)
        paused_progress = paused["selected"]["progress"]
        self.assertEqual(paused_progress["state"], FactoryState.PAUSED.value)
        self.assertEqual(paused_progress["effective_state"], FactoryState.BASELINING.value)
        self.assertEqual(paused_progress["percent"], progress["percent"])
        self.assertEqual(paused_progress["outcome"], "paused")

    async def test_snapshot_fingerprint_changes_only_when_durable_content_changes(self):
        run = await self._run()
        first = await self.service.snapshot(user_id="user-1", run_id=run.id)
        second = await self.service.snapshot(user_id="user-1", run_id=run.id)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertGreaterEqual(second["generated_at_ms"], first["generated_at_ms"])

        await self.store.append_user_event(
            run_id=run.id,
            event_type="user.message",
            payload={"content": "new durable event"},
            idempotency_key="new-event",
        )
        changed = await self.service.snapshot(user_id="user-1", run_id=run.id)
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])

    async def test_explicit_run_id_cannot_cross_owner_boundary(self):
        run = await self._run()
        other = await self._run(user_id="user-2", workspace_id="workspace-2", key="other-run")
        events = await self.service.activity_since(
            user_id="user-1", run_id=run.id, after_sequence=0
        )
        self.assertEqual([event["sequence"] for event in events], [1])
        with self.assertRaises(KeyError):
            await self.service.snapshot(user_id="user-1", run_id=other.id)
        with self.assertRaises(KeyError):
            await self.service.activity_since(user_id="user-1", run_id=other.id, after_sequence=0)


class FactoryObservabilityApiTests(unittest.IsolatedAsyncioTestCase):
    def _request(self):
        return SimpleNamespace(is_disconnected=AsyncMock(return_value=False))

    async def test_snapshot_uses_admin_identity_and_maps_inaccessible_run_to_404(self):
        request = self._request()
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        service = SimpleNamespace(
            snapshot=AsyncMock(
                return_value={
                    "version": 1,
                    "runs": [],
                    "selected": None,
                    "fingerprint": "a" * 64,
                    "generated_at_ms": 1,
                }
            )
        )
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "factory_observability", service),
        ):
            result = await mcp_router.get_factory_observability_snapshot(
                request, run_id="factory-1", run_limit=7
            )
        self.assertEqual(result["fingerprint"], "a" * 64)
        admin.assert_called_once_with(request)
        service.snapshot.assert_awaited_once_with(
            user_id="admin-1", run_id="factory-1", run_limit=7
        )

        service.snapshot = AsyncMock(side_effect=KeyError("factory run not found"))
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "factory_observability", service),
        ):
            with self.assertRaises(HTTPException) as raised:
                await mcp_router.get_factory_observability_snapshot(
                    request, run_id="other-user-run", run_limit=20
                )
        self.assertEqual(raised.exception.status_code, 404)

    async def test_stream_emits_fine_grained_activity_and_progress_before_changed_snapshot(self):
        request = self._request()
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        event_one = {
            "event_id": "event-1",
            "cycle_id": "cycle-1",
            "sequence": 1,
            "actor": "SYSTEM",
            "event_type": "state.transition",
            "from_state": "MISSION",
            "to_state": "RECOVERING",
            "payload": {},
            "created_at": 1,
        }
        event_two = {
            **event_one,
            "event_id": "event-2",
            "sequence": 2,
            "from_state": "RECOVERING",
            "to_state": "BASELINING",
        }
        progress_one = {
            "percent": 4,
            "state": "RECOVERING",
            "effective_state": "RECOVERING",
            "phase_index": 2,
            "phase_count": 25,
            "outcome": "running",
            "terminal": False,
            "basis": "server_state_machine",
            "updated_at_ms": 1,
        }
        progress_two = {
            **progress_one,
            "percent": 8,
            "state": "BASELINING",
            "effective_state": "BASELINING",
            "phase_index": 3,
            "updated_at_ms": 2,
        }
        first = {
            "version": 1,
            "runs": [],
            "selected": {
                "run_id": "factory-1",
                "events": [event_one],
                "progress": progress_one,
                "summary": {"last_event_sequence": 1},
            },
            "fingerprint": "1" * 64,
            "generated_at_ms": 1,
        }
        second = {
            **first,
            "selected": {
                "run_id": "factory-1",
                "events": [event_one, event_two],
                "progress": progress_two,
                "summary": {"last_event_sequence": 2},
            },
            "fingerprint": "2" * 64,
            "generated_at_ms": 2,
        }
        service = SimpleNamespace(
            snapshot=AsyncMock(side_effect=[first, second, second]),
            activity_since=AsyncMock(return_value=[event_two]),
        )
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "factory_observability", service),
            patch.object(mcp_router.asyncio, "sleep", new=AsyncMock(return_value=None)),
        ):
            response = await mcp_router.stream_factory_observability(
                request, run_id="factory-1", run_limit=9
            )
            iterator = response.body_iterator.__aiter__()
            self.assertEqual(await iterator.__anext__(), "retry: 1500\n\n")
            initial_snapshot = await iterator.__anext__()
            initial_progress = await iterator.__anext__()
            activity = await iterator.__anext__()
            progress = await iterator.__anext__()
            changed_snapshot = await iterator.__anext__()
            await iterator.aclose()
        self.assertIn("event: snapshot", initial_snapshot)
        self.assertIn("event: progress", initial_progress)
        self.assertIn("event: activity", activity)
        self.assertIn('"event_id":"event-2"', activity)
        self.assertIn("event: progress", progress)
        self.assertIn('"percent":8', progress)
        self.assertIn("event: snapshot", changed_snapshot)
        service.activity_since.assert_awaited_once_with(
            user_id="admin-1", run_id="factory-1", after_sequence=1, limit=500
        )

    async def test_stream_emits_retry_then_owner_scoped_changed_snapshot(self):
        request = self._request()
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        snapshot = {
            "version": 1,
            "runs": [],
            "selected": None,
            "fingerprint": "b" * 64,
            "generated_at_ms": 1,
        }
        service = SimpleNamespace(snapshot=AsyncMock(return_value=snapshot))
        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "factory_observability", service),
        ):
            response = await mcp_router.stream_factory_observability(
                request, run_id="factory-1", run_limit=9
            )
            iterator = response.body_iterator.__aiter__()
            retry = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            event = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()
        self.assertEqual(retry, "retry: 1500\n\n")
        self.assertIn("event: snapshot", event)
        self.assertIn('"fingerprint":"' + "b" * 64 + '"', event)
        service.snapshot.assert_awaited_once_with(
            user_id="admin-1", run_id="factory-1", run_limit=9
        )
        admin.assert_called_once_with(request)
