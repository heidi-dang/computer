import os
import unittest
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
        invalid = (AGENT_REGISTRY[0], AGENT_REGISTRY[1].__class__(
            "bad", AGENT_REGISTRY[1].role, "bad", can_delegate=True
        ))
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
                DelegationRequest("heidi", "planner", requested_capabilities=frozenset({Capability.WRITE_FILES})),
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
        diagnostic = shadow_route("Please inspect the repository", "", FlowDeckConfig(
            enabled=True, mode=FlowDeckMode.SHADOW
        ))
        self.assertEqual(diagnostic.metadata["execution"], "native_cptr_only")

    def test_disabled_fast_path_does_not_invoke_router(self):
        os.environ.pop("CPTR_FLOWDECK_ENABLED", None)
        os.environ.pop("FLOWDECK_ENABLED", None)
        with patch("cptr.flowdeck.gateway.shadow_route", side_effect=AssertionError("router invoked")):
            self.assertIsNone(observe_request(content="write this file"))

    def test_shadow_errors_are_isolated(self):
        os.environ["CPTR_FLOWDECK_ENABLED"] = "true"
        os.environ["CPTR_FLOWDECK_MODE"] = "shadow"
        with patch("cptr.flowdeck.gateway.shadow_route", side_effect=RuntimeError("diagnostic failure")):
            self.assertIsNone(observe_request(content="inspect this"))


if __name__ == "__main__":
    unittest.main()