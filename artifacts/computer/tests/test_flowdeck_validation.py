import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import FlowDeckMode
from cptr.flowdeck.coordinator import PlannedDelegation
from cptr.flowdeck.validation import validate_pre_execution


def plan(*specialists):
    return tuple(
        PlannedDelegation(
            specialist_id=specialist,
            objective="bounded review",
            capabilities=frozenset(),
        )
        for specialist in specialists
    )


def config(**overrides):
    values = {
        "enabled": True,
        "coordinator_enabled": True,
        "mode": FlowDeckMode.CONTROLLED,
        "governance": "strict",
    }
    values.update(overrides)
    return FlowDeckConfig(**values)


class PreExecutionValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def validate(self, task, **kwargs):
        settings = {"config": config()}
        settings.update(kwargs)
        return await validate_pre_execution(
            task=task,
            workspace=str(self.workspace),
            model="configured-cptr-model",
            plan=plan("reviewer"),
            **settings,
        )

    async def test_valid_task_proceeds_after_real_workspace_validation(self):
        result = await self.validate("review the implementation")
        self.assertEqual(result.outcome, "passed")
        self.assertTrue(result.facts["workspace_exists"])
        self.assertFalse(result.facts["git_available"])
        self.assertTrue(result.fingerprint)

    async def test_incorrect_model_switch_assumption_is_rejected(self):
        result = await self.validate("review the implementation and switch provider automatically")
        self.assertEqual(result.outcome, "rejected")
        self.assertIn("frozen", result.reason)

    async def test_ambiguous_task_requires_clarification(self):
        result = await validate_pre_execution(
            task="hi",
            workspace=str(self.workspace),
            model="configured-cptr-model",
            plan=(),
            config=config(),
        )
        self.assertEqual(result.outcome, "clarification")

    async def test_frozen_boundary_conflict_is_rejected_before_clarification(self):
        result = await validate_pre_execution(
            task="enable MCP for this task",
            workspace=str(self.workspace),
            model="configured-cptr-model",
            plan=(),
            config=config(),
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertIn("frozen", result.reason)

    async def test_unavailable_capability_is_rejected(self):
        result = await self.validate("review this using unrestricted network access")
        self.assertEqual(result.outcome, "rejected")
        self.assertIn("unavailable", result.reason)

    async def test_no_workspace_or_model_fails_closed(self):
        missing = await validate_pre_execution(
            task="review the implementation",
            workspace=str(self.workspace / "missing"),
            model="configured-cptr-model",
            plan=plan("reviewer"),
            config=config(),
        )
        self.assertEqual(missing.outcome, "rejected")
        no_model = await validate_pre_execution(
            task="review the implementation",
            workspace=str(self.workspace),
            model="",
            plan=plan("reviewer"),
            config=config(),
        )
        self.assertEqual(no_model.outcome, "rejected")

    async def test_cancellation_during_validation_is_not_reinterpreted(self):
        with patch(
            "cptr.flowdeck.validation._workspace_facts",
            side_effect=lambda _workspace: (_ for _ in ()).throw(asyncio.CancelledError()),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await self.validate("review the implementation")

    async def test_mutation_policy_rejection_precedes_any_execution(self):
        result = await self.validate(
            "edit the implementation",
            config=config(mutating_agents=False),
        )
        self.assertEqual(result.outcome, "rejected")
        self.assertIn("mutation", result.reason)


if __name__ == "__main__":
    unittest.main()