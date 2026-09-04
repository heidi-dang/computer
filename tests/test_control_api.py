import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from cptr.routers.control import (
    ApprovalRequest,
    AutonomousCreateRequest,
    TaskCreateRequest,
    TaskExecutionPolicy,
    ReviewDecisionRequest,
    _is_qualified_model_id,
    _monitor_summary,
    approve_autonomous,
    create_autonomous,
    create_task,
    decide_task_review,
    get_task_review,
    get_autonomous_evidence,
    list_models,
)
from cptr.services.supervisor import (
    EvidenceRecord,
    MonitorState,
    MonitorStatus,
    ScopeRecord,
    ScopeStatus,
)


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_listing_only_advertises_ids_accepted_by_delegation_gate(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        connections = [
            {"enabled": True, "prefix_id": "provider"},
            {"enabled": True, "prefix_id": ""},
        ]
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.chat._get_connections", new=AsyncMock(return_value=connections)),
            patch(
                "cptr.routers.chat._get_connection_models",
                new=AsyncMock(return_value=["model_1"]),
            ),
            patch(
                "cptr.utils.agents.detection.get_available_agent_model_entries",
                new=AsyncMock(
                    return_value=[
                        {"id": "agent:codex/model_2", "name": "Codex model"},
                        {"id": "bare-agent-model", "name": "Invalid bare model"},
                    ]
                ),
            ),
            patch(
                "cptr.routers.control._default_model",
                new=AsyncMock(return_value="provider/model_1"),
            ),
        ):
            result = await list_models(request)

        model_ids = [item["model_id"] for item in result["models"]]
        self.assertEqual(model_ids, ["provider/model_1", "agent:codex/model_2"])
        self.assertTrue(all(_is_qualified_model_id(model_id) for model_id in model_ids))
        self.assertTrue(result["models"][0]["default"])

    async def test_task_creation_delegates_to_agent_service_with_stable_contract(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        agent = SimpleNamespace(
            start_task=AsyncMock(
                return_value={
                    "id": "task_1",
                    "workspace_id": "ws_1",
                    "status": "RUNNING",
                }
            )
        )
        services = (agent, SimpleNamespace())
        body = TaskCreateRequest(
            workspace_id="ws_1",
            prompt="Run the tests allow:delegate",
            model_id="provider/model_1",
            idempotency_key="request_1",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            patch("cptr.routers.control._services", return_value=services),
        ):
            result = await create_task(request, body)

        self.assertEqual(result["id"], "task_1")
        agent.start_task.assert_awaited_once_with(
            user_id="user_1",
            workspace_id="ws_1",
            prompt="Run the tests allow:delegate",
            model_id="provider/model_1",
            idempotency_key="request_1",
            execution_policy={
                "allow_file_writes": True,
                "allow_commands": True,
                "allow_network": False,
                "allow_package_install": False,
            },
            request=request,
        )

    async def test_task_creation_uses_qualified_default_model_after_delegation_opt_in(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        agent = SimpleNamespace(
            start_task=AsyncMock(
                return_value={"id": "task_default", "workspace_id": "ws_1", "status": "RUNNING"}
            )
        )
        body = TaskCreateRequest(
            workspace_id="ws_1",
            prompt="Run the tests allow:delegate",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            patch(
                "cptr.routers.control._default_model",
                new=AsyncMock(return_value="provider/default-model"),
            ),
            patch("cptr.routers.control._services", return_value=(agent, SimpleNamespace())),
        ):
            result = await create_task(request, body)

        self.assertEqual(result["id"], "task_default")
        self.assertEqual(agent.start_task.await_args.kwargs["model_id"], "provider/default-model")

    async def test_task_creation_rejects_missing_delegation_marker_before_agent_start(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        services = patch("cptr.routers.control._services")
        body = TaskCreateRequest(
            workspace_id="ws_1",
            prompt="Audit this workspace",
            model_id="provider/model_1",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            services as service_factory,
            self.assertRaises(HTTPException) as rejected,
        ):
            await create_task(request, body)

        self.assertEqual(rejected.exception.status_code, 422)
        self.assertEqual(rejected.exception.detail["code"], "DELEGATION_NOT_ALLOWED")
        service_factory.assert_not_called()

    async def test_task_creation_rejects_unqualified_or_missing_delegation_model(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        for model_id, expected_code in (
            (None, "DELEGATION_MODEL_REQUIRED"),
            ("bare-model", "DELEGATION_MODEL_NOT_QUALIFIED"),
        ):
            with self.subTest(model_id=model_id):
                body = TaskCreateRequest(
                    workspace_id="ws_1",
                    prompt="Audit this workspace allow:delegate",
                    model_id=model_id,
                )
                with (
                    patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
                    patch(
                        "cptr.routers.control._ensure_workspace",
                        new=AsyncMock(return_value=object()),
                    ),
                    patch("cptr.routers.control._default_model", new=AsyncMock(return_value=None)),
                    patch("cptr.routers.control._services") as service_factory,
                    self.assertRaises(HTTPException) as rejected,
                ):
                    await create_task(request, body)
                self.assertEqual(rejected.exception.detail["code"], expected_code)
                service_factory.assert_not_called()

    async def test_task_creation_accepts_explicit_agent_profile_model(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        agent = SimpleNamespace(
            start_task=AsyncMock(
                return_value={"id": "task_agent", "workspace_id": "ws_1", "status": "RUNNING"}
            )
        )
        body = TaskCreateRequest(
            workspace_id="ws_1",
            prompt="Use the approved coding profile allow:delegate",
            model_id="agent:codex/gpt-5.1-codex",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            patch("cptr.routers.control._services", return_value=(agent, SimpleNamespace())),
        ):
            await create_task(request, body)

        self.assertEqual(
            agent.start_task.await_args.kwargs["model_id"], "agent:codex/gpt-5.1-codex"
        )

    async def test_task_creation_forwards_server_enforced_execution_policy(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        agent = SimpleNamespace(
            start_task=AsyncMock(
                return_value={"id": "task_2", "workspace_id": "ws_1", "status": "RUNNING"}
            )
        )
        body = TaskCreateRequest(
            workspace_id="ws_1",
            prompt="Audit without installs or network allow:delegate",
            model_id="provider/model_1",
            execution_policy=TaskExecutionPolicy(
                allow_file_writes=False,
                allow_commands=True,
                allow_network=False,
                allow_package_install=False,
            ),
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            patch("cptr.routers.control._services", return_value=(agent, SimpleNamespace())),
        ):
            await create_task(request, body)

        agent.start_task.assert_awaited_once_with(
            user_id="user_1",
            workspace_id="ws_1",
            prompt="Audit without installs or network allow:delegate",
            model_id="provider/model_1",
            idempotency_key=None,
            execution_policy={
                "allow_file_writes": False,
                "allow_commands": True,
                "allow_network": False,
                "allow_package_install": False,
            },
            request=request,
        )

    async def test_task_review_endpoint_forwards_read_authorized_lookup(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        agent = SimpleNamespace(
            get_task_review=AsyncMock(
                return_value={
                    "task_id": "task_1",
                    "status": "REVIEW_REQUIRED",
                    "review": {"status": "REQUIRED"},
                    "diff": {"files": []},
                }
            )
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")) as user,
            patch("cptr.routers.control._services", return_value=(agent, SimpleNamespace())),
        ):
            result = await get_task_review(request, "task_1")

        self.assertEqual(result["review"]["status"], "REQUIRED")
        user.assert_awaited_once_with(request, "task:read")
        agent.get_task_review.assert_awaited_once_with("task_1", user_id="user_1")

    async def test_review_decision_endpoint_forwards_scoped_user_action(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        agent = SimpleNamespace(
            decide_review=AsyncMock(
                return_value={
                    "id": "task_1",
                    "status": "COMPLETE",
                    "review": {"status": "ACCEPTED"},
                }
            )
        )
        body = ReviewDecisionRequest(
            decision="ACCEPT",
            note="Reviewed the diff",
            idempotency_key="review_1",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._services", return_value=(agent, SimpleNamespace())),
        ):
            result = await decide_task_review(request, "task_1", body)

        self.assertEqual(result["review"]["status"], "ACCEPTED")
        agent.decide_review.assert_awaited_once_with(
            "task_1",
            user_id="user_1",
            decision="ACCEPT",
            note="Reviewed the diff",
            idempotency_key="review_1",
        )

    async def test_monitor_summary_keeps_original_goal_and_counts_verified_scopes(self):
        monitor = MonitorState(
            monitor_id="mon_1",
            goal_id="goal_1",
            user_id="user_1",
            workspace_id="ws_1",
            original_goal="Ship feature",
            original_acceptance_criteria=["Tests pass", "Diff is reviewed"],
            model_id="model_1",
            scopes=[
                ScopeRecord(
                    "scope_1", "Tests pass", "Tests pass", ["Tests pass"], ScopeStatus.VERIFIED
                ),
                ScopeRecord(
                    "scope_2", "Diff is reviewed", "Diff is reviewed", ["Diff is reviewed"]
                ),
            ],
        )
        result = _monitor_summary(monitor)
        self.assertEqual(result["scope_count"], 2)
        self.assertEqual(result["verified_count"], 1)
        self.assertEqual(result["original_goal"], "Ship feature")
        self.assertEqual(result["acceptance_criteria"], ["Tests pass", "Diff is reviewed"])

    async def test_monitor_creation_schedules_server_side_loop(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        monitor = MonitorState(
            monitor_id="mon_1",
            goal_id="goal_1",
            user_id="user_1",
            workspace_id="ws_1",
            original_goal="Ship feature",
            original_acceptance_criteria=["Tests pass"],
            model_id="model_1",
            scopes=[ScopeRecord("scope_1", "Tests pass", "Tests pass", ["Tests pass"])],
        )
        supervisor = SimpleNamespace(create_goal=AsyncMock(return_value=monitor))
        body = AutonomousCreateRequest(
            workspace_id="ws_1",
            goal="Ship feature allow:delegate",
            acceptance_criteria=["Tests pass"],
            model_id="provider/model_1",
            idempotency_key="goal_1",
            execution_policy=TaskExecutionPolicy(
                allow_file_writes=True,
                allow_commands=True,
                allow_network=False,
                allow_package_install=False,
            ),
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._ensure_workspace", new=AsyncMock(return_value=object())),
            patch("cptr.routers.control._services", return_value=(SimpleNamespace(), supervisor)),
            patch("cptr.routers.control._schedule_monitor") as schedule,
        ):
            result = await create_autonomous(request, body)

        self.assertEqual(result["monitor_id"], "mon_1")
        self.assertEqual(result["status"], "RUNNING")
        supervisor.create_goal.assert_awaited_once_with(
            user_id="user_1",
            workspace_id="ws_1",
            goal="Ship feature allow:delegate",
            acceptance_criteria=["Tests pass"],
            model_id="provider/model_1",
            idempotency_key="goal_1",
            execution_policy={
                "allow_file_writes": True,
                "allow_commands": True,
                "allow_network": False,
                "allow_package_install": False,
            },
        )
        schedule.assert_called_once_with(request.app, "mon_1")

    async def test_evidence_endpoint_reads_dedicated_evidence_records(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        monitor = MonitorState(
            monitor_id="mon_1",
            goal_id="goal_1",
            user_id="user_1",
            workspace_id="ws_1",
            original_goal="Ship feature",
            original_acceptance_criteria=["Tests pass"],
            model_id="model_1",
            scopes=[ScopeRecord("scope_1", "Tests pass", "Tests pass", ["Tests pass"])],
        )
        supervisor = SimpleNamespace(
            store=SimpleNamespace(
                get_monitor=AsyncMock(return_value=monitor),
                list_evidence=AsyncMock(
                    return_value=[
                        EvidenceRecord(
                            "e_1", "mon_1", "scope_1", "verification_result", {"passed": True}, 1
                        )
                    ]
                ),
            )
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._services", return_value=(SimpleNamespace(), supervisor)),
        ):
            result = await get_autonomous_evidence(request, "mon_1")

        self.assertEqual(result["evidence"][0]["evidence_id"], "e_1")
        self.assertEqual(result["evidence"][0]["kind"], "verification_result")

    async def test_approved_monitor_is_rescheduled(self):
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
        monitor = MonitorState(
            monitor_id="mon_1",
            goal_id="goal_1",
            user_id="user_1",
            workspace_id="ws_1",
            original_goal="Publish",
            original_acceptance_criteria=["Push is approved"],
            model_id="model_1",
            scopes=[ScopeRecord("scope_1", "Push", "Push", ["Push"], ScopeStatus.PENDING)],
            status=MonitorStatus.APPROVAL_REQUIRED,
            current_scope_id="scope_1",
            approval_id="approval_1",
        )
        supervisor = SimpleNamespace(
            store=SimpleNamespace(get_monitor=AsyncMock(return_value=monitor)),
            approve=AsyncMock(
                return_value=MonitorState(
                    **{**monitor.__dict__, "status": MonitorStatus.RUNNING, "approval_id": None}
                )
            ),
        )
        body = ApprovalRequest(
            approval_id="approval_1",
            approved=True,
            note="Approved after reviewing the bounded deployment evidence.",
        )
        with (
            patch("cptr.routers.control._user", new=AsyncMock(return_value="user_1")),
            patch("cptr.routers.control._services", return_value=(SimpleNamespace(), supervisor)),
            patch("cptr.routers.control._schedule_monitor") as schedule,
        ):
            result = await approve_autonomous(request, "mon_1", body)

        self.assertEqual(result["status"], "RUNNING")
        supervisor.approve.assert_awaited_once_with(
            "mon_1",
            approval_id="approval_1",
            approved=True,
            note="Approved after reviewing the bounded deployment evidence.",
        )
        schedule.assert_called_once_with(request.app, "mon_1")


if __name__ == "__main__":
    unittest.main()
