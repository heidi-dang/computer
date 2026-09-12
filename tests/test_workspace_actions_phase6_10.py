import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cptr.events import EVENTS
from cptr.services.workspace_actions import (
    READ_ACTIONS,
    WRITE_ACTIONS,
    WorkspaceActionService,
    _safe_environment_version,
)


def _workspace(root: str = "/tmp/workspace"):
    return SimpleNamespace(
        id="ws-phase10",
        user_id="user-1",
        path=root,
        name="Phase 10",
        slug="phase-10",
        workspace_type="project",
        data={},
        created_at=1,
        updated_at=2,
    )


class WorkspaceActionPhaseSixToTenTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_actions_are_explicitly_classified(self):
        self.assertIn("environment_versions", READ_ACTIONS)
        self.assertIn("tasks", READ_ACTIONS)
        self.assertIn("task_summary", READ_ACTIONS)
        self.assertIn("task_create", WRITE_ACTIONS)
        self.assertIn("task_update_status", WRITE_ACTIONS)
        self.assertIn("task_pin_repository", WRITE_ACTIONS)
        self.assertIn("task_add_evidence", WRITE_ACTIONS)

    async def test_task_create_delegates_to_existing_service_and_publishes_task_axis(self):
        service = WorkspaceActionService()
        workspace = _workspace()
        service._resolve = AsyncMock(return_value=workspace)
        service._invalidate_axis = AsyncMock()

        with patch(
            "cptr.services.workspace_actions.workspace_task_service.create_task",
            new=AsyncMock(return_value={"id": "wst-1", "status": "OPEN"}),
        ) as create:
            result = await service.task_create(
                user_id="user-1",
                reference="ws-phase10",
                title="Finish Workspace UI",
                description="phase 10",
                metadata={"source": "test"},
            )

        self.assertEqual(result["task"]["id"], "wst-1")
        create.assert_awaited_once_with(
            user_id="user-1",
            workspace_id="ws-phase10",
            title="Finish Workspace UI",
            description="phase 10",
            metadata={"source": "test"},
        )
        service._invalidate_axis.assert_awaited_once_with(
            user_id="user-1",
            workspace_id="ws-phase10",
            axis="task",
        )

    async def test_task_evidence_delegates_without_creating_second_verification_engine(self):
        service = WorkspaceActionService()
        workspace = _workspace()
        service._resolve = AsyncMock(return_value=workspace)
        service._invalidate_axis = AsyncMock()

        with patch(
            "cptr.services.workspace_actions.workspace_task_service.add_evidence",
            new=AsyncMock(return_value={"id": "evidence-1", "status": "PASSED"}),
        ) as add_evidence:
            result = await service.task_add_evidence(
                user_id="user-1",
                reference="ws-phase10",
                task_id="wst-1",
                kind="test",
                status="PASSED",
                summary="focused tests passed",
                command="pytest -q",
            )

        self.assertEqual(result["evidence"]["id"], "evidence-1")
        add_evidence.assert_awaited_once_with(
            user_id="user-1",
            workspace_id="ws-phase10",
            task_id="wst-1",
            kind="test",
            status="PASSED",
            summary="focused tests passed",
            worker_id=None,
            repo_path=None,
            command="pytest -q",
            details=None,
            fingerprint=None,
        )
        service._invalidate_axis.assert_awaited_once_with(
            user_id="user-1",
            workspace_id="ws-phase10",
            axis="task",
        )

    async def test_task_summary_delegates_to_authoritative_workspace_task_service(self):
        service = WorkspaceActionService()
        workspace = _workspace()
        service._resolve = AsyncMock(return_value=workspace)

        with patch(
            "cptr.services.workspace_actions.workspace_task_service.summary",
            new=AsyncMock(return_value={"task": {"id": "wst-1"}, "verification": {"verified": True}}),
        ) as summary:
            result = await service.task_summary(
                user_id="user-1",
                reference="ws-phase10",
                task_id="wst-1",
            )

        self.assertTrue(result["summary"]["verification"]["verified"])
        summary.assert_awaited_once_with(
            user_id="user-1",
            workspace=workspace,
            task_id="wst-1",
        )

    def test_environment_version_serializer_returns_names_and_refs_not_secret_values(self):
        version = SimpleNamespace(
            id="envv-1",
            version_number=3,
            digest="sha256:test",
            runtime_profile="node",
            environment_variables={"API_TOKEN": "secret-value", "PUBLIC_URL": "https://example.test"},
            packages={"node": "24"},
            settings={"mode": "dev"},
            credential_refs=[
                {
                    "logical_name": "github",
                    "source_type": "broker",
                    "target_env_var": "GITHUB_TOKEN",
                    "consumers": ["build"],
                    "secret": "must-not-leak",
                }
            ],
            parent_version_id="envv-0",
            created_at_ms=123,
        )

        safe = _safe_environment_version(version)

        self.assertEqual(safe["environment_variable_names"], ["API_TOKEN", "PUBLIC_URL"])
        self.assertNotIn("secret-value", repr(safe))
        self.assertNotIn("must-not-leak", repr(safe))
        self.assertEqual(safe["credential_refs"][0]["logical_name"], "github")

    def test_workspace_task_changed_event_is_registered(self):
        self.assertEqual(EVENTS.WORKSPACE_TASK_CHANGED.name, "workspace.task.changed")


if __name__ == "__main__":
    unittest.main()
