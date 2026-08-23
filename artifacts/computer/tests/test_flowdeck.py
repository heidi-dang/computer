import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import (
    Capability,
    DelegationRequest,
    FlowDeckMode,
    GovernanceVerdict,
    RouteStrategy,
)
from cptr.flowdeck.delegation import validate_delegation
from cptr.flowdeck.errors import DelegationPolicyError, RegistryError
from cptr.flowdeck.gateway import observe_request
from cptr.flowdeck.governance import evaluate_capabilities
from cptr.flowdeck.registry import AGENT_REGISTRY, validate_registry
from cptr.flowdeck.router import classify_request, shadow_route

MATRIX_PATHWAYS = (
    "authenticated_chat_creation",
    "queued_follow_up",
    "cancellation",
    "tool_approval",
    "socketio_streaming",
    "external_agent_selection",
    "restart_reconciliation",
    "files",
    "terminals",
    "git",
    "browser",
    "mcp",
)


class NativeRequestProbe:
    """Request-level oracle for the non-authoritative shadow boundary."""

    def __init__(self, pathway):
        self.pathway = pathway
        self.response = {
            "status": 200,
            "pathway": pathway,
            "body": {"ok": True, "request_id": "stable"},
        }
        self.provider_calls = []
        self.tool_calls = []
        self.filesystem = {"workspace.txt": "before"}
        self.events = []

    def execute(self):
        self.provider_calls.append(("native-provider", self.pathway))
        if self.pathway in {"files", "git", "terminals"}:
            self.filesystem["workspace.txt"] = f"{self.pathway}:native"
        if self.pathway in {"tool_approval", "mcp"}:
            self.tool_calls.append(("native-tool", self.pathway))
        self.events.append(("events:chat", self.pathway))
        return self.snapshot()

    def snapshot(self):
        return {
            "response": deepcopy(self.response),
            "provider_calls": list(self.provider_calls),
            "tool_calls": list(self.tool_calls),
            "filesystem": deepcopy(self.filesystem),
            "events": list(self.events),
        }


class FlowDeckContractsTests(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.env)

    def test_registry_is_canonical_and_valid(self):
        validate_registry()
        self.assertEqual(AGENT_REGISTRY[0].id, "heidi")
        self.assertTrue(AGENT_REGISTRY[0].can_delegate)
        self.assertEqual(len({agent.id for agent in AGENT_REGISTRY}), len(AGENT_REGISTRY))

    def test_registry_rejects_specialist_delegation(self):
        invalid = (
            AGENT_REGISTRY[0],
            AGENT_REGISTRY[1].__class__("bad", AGENT_REGISTRY[1].role, "bad", can_delegate=True),
        )
        with self.assertRaises(RegistryError):
            validate_registry(invalid)

    def test_delegation_only_allows_heidi_to_specialist_at_depth_one(self):
        config = FlowDeckConfig(enabled=True, mode=FlowDeckMode.SHADOW)
        validate_delegation(DelegationRequest("heidi", "researcher"), config)
        with self.assertRaises(DelegationPolicyError):
            validate_delegation(DelegationRequest("researcher", "planner"), config)
        with self.assertRaises(DelegationPolicyError):
            validate_delegation(DelegationRequest("heidi", "planner", depth=2), config)
        with self.assertRaises(DelegationPolicyError):
            validate_delegation(
                DelegationRequest(
                    "heidi", "planner", requested_capabilities=frozenset({Capability.WRITE_FILES})
                ),
                config,
            )

    def test_governance_denies_high_risk_and_unknown_in_strict_mode(self):
        config = FlowDeckConfig(enabled=True, mode=FlowDeckMode.SHADOW, governance="strict")
        decisions = evaluate_capabilities(
            {Capability.WRITE_FILES, Capability.READ_FILES}, config=config
        )
        self.assertEqual(
            {item.capability: item.verdict for item in decisions},
            {
                Capability.READ_FILES: GovernanceVerdict.DENY,
                Capability.WRITE_FILES: GovernanceVerdict.DENY,
            },
        )

    def test_router_is_deterministic_and_side_effect_free(self):
        first = classify_request("Please research the architecture and inspect the repository")
        second = classify_request("Please research the architecture and inspect the repository")
        self.assertEqual(first, second)
        self.assertEqual(first.strategy, RouteStrategy.SPECIALIST)
        self.assertEqual(first.specialist_ids, ("architect", "researcher", "mapper"))
        diagnostic = shadow_route(
            "Please inspect the repository",
            "",
            FlowDeckConfig(enabled=True, mode=FlowDeckMode.SHADOW),
        )
        self.assertEqual(diagnostic.metadata["execution"], "native_cptr_only")

    def test_disabled_fast_path_does_not_invoke_router(self):
        os.environ.pop("CPTR_FLOWDECK_ENABLED", None)
        os.environ.pop("FLOWDECK_ENABLED", None)
        with patch(
            "cptr.flowdeck.gateway.shadow_route", side_effect=AssertionError("router invoked")
        ):
            self.assertIsNone(observe_request(content="write this file"))

    def test_shadow_errors_are_isolated(self):
        os.environ["CPTR_FLOWDECK_ENABLED"] = "true"
        os.environ["CPTR_FLOWDECK_MODE"] = "shadow"
        with patch(
            "cptr.flowdeck.gateway.shadow_route", side_effect=RuntimeError("diagnostic failure")
        ):
            self.assertIsNone(observe_request(content="inspect this"))

    def test_request_matrix_covers_all_cptr_boundaries(self):
        self.assertEqual(len(MATRIX_PATHWAYS), 12)
        self.assertEqual(len(set(MATRIX_PATHWAYS)), len(MATRIX_PATHWAYS))

    def test_shadow_mode_matches_disabled_request_snapshots(self):
        """Shadow diagnostics cannot alter response or native side effects."""
        for pathway in MATRIX_PATHWAYS:
            with self.subTest(pathway=pathway):
                disabled = NativeRequestProbe(pathway)
                with patch.dict(os.environ, {}, clear=True):
                    baseline = disabled.execute()
                    self.assertIsNone(observe_request(content=f"request:{pathway}"))

                shadow = NativeRequestProbe(pathway)
                with patch.dict(
                    os.environ,
                    {
                        "CPTR_FLOWDECK_ENABLED": "true",
                        "CPTR_FLOWDECK_MODE": "shadow",
                    },
                    clear=True,
                ):
                    before_observation = shadow.snapshot()
                    diagnostic = observe_request(
                        content=f"request:{pathway}",
                        model_id="native-model",
                        user_id="user-1",
                        workspace="/workspace",
                    )
                    after_observation = shadow.snapshot()
                    self.assertIsNotNone(diagnostic)
                    self.assertEqual(before_observation, after_observation)
                    observed = shadow.execute()

                self.assertEqual(baseline, observed)

    def test_shadow_diagnostics_cannot_invoke_native_effects(self):
        """The diagnostic route cannot become a provider or tool owner."""
        effects = []

        def diagnostic_route(content, model_id, config):
            effects.append(("diagnostic", content, model_id))
            return shadow_route(content, model_id, config)

        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "shadow",
                },
                clear=True,
            ),
            patch("cptr.flowdeck.gateway.shadow_route", side_effect=diagnostic_route),
        ):
            result = observe_request(
                content="inspect files and use browser",
                model_id="native-model",
                user_id="user-1",
                workspace="/workspace",
            )

        self.assertIsNotNone(result)
        self.assertEqual(effects, [("diagnostic", "inspect files and use browser", "native-model")])
        self.assertEqual(result.metadata["execution"], "native_cptr_only")

    def test_shadow_failure_keeps_authoritative_request_result(self):
        """A diagnostic failure is baseline evidence, never a CPTR error."""
        for pathway in MATRIX_PATHWAYS:
            with self.subTest(pathway=pathway):
                probe = NativeRequestProbe(pathway)
                expected = probe.execute()
                with (
                    patch.dict(
                        os.environ,
                        {
                            "CPTR_FLOWDECK_ENABLED": "true",
                            "CPTR_FLOWDECK_MODE": "shadow",
                        },
                        clear=True,
                    ),
                    patch(
                        "cptr.flowdeck.gateway.shadow_route",
                        side_effect=RuntimeError("diagnostic-only failure"),
                    ),
                ):
                    self.assertIsNone(observe_request(content=f"request:{pathway}"))
                self.assertEqual(expected, probe.snapshot())


if __name__ == "__main__":
    unittest.main()
