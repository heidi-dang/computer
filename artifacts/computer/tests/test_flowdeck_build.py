import os
import unittest
from unittest.mock import patch

from cptr.flowdeck.build import (
    BuildContractError,
    build_contract_is_satisfied,
    create_build_request,
    parse_build_request,
)
from cptr.flowdeck.coordinator import classify_coordinator_request


class BuildContractTests(unittest.TestCase):
    def test_explicit_build_creates_reviewable_brief_architecture_and_contract(self):
        request = parse_build_request("/build Build an inventory management system")
        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.objective, "Build an inventory management system")
        self.assertEqual(request.brief.version, 1)
        self.assertIn("primary_flow", request.completion.required_checks)
        self.assertEqual(request.architecture.frontend, "detect-existing")

    def test_defaults_are_server_configurable(self):
        with patch.dict(os.environ, {"CPTR_BUILD_FRONTEND": "react-vite"}):
            request = create_build_request("Create a dashboard")
        self.assertEqual(request.architecture.frontend, "react-vite")

    def test_non_build_text_is_not_reinterpreted(self):
        self.assertIsNone(parse_build_request("build an inventory management system"))

    def test_empty_build_fails_closed(self):
        with self.assertRaises(BuildContractError):
            parse_build_request("/build")

    def test_completion_requires_every_authoritative_check(self):
        request = create_build_request("Create a dashboard")
        self.assertFalse(build_contract_is_satisfied(request.completion, {}))
        evidence = {check: "VERIFIED" for check in request.completion.required_checks}
        self.assertTrue(build_contract_is_satisfied(request.completion, evidence))
        evidence["runtime_health"] = "FAILED"
        self.assertFalse(build_contract_is_satisfied(request.completion, evidence))

    def test_build_plan_uses_only_read_only_phase_one_specialists(self):
        plan = classify_coordinator_request("/build Create a dashboard")
        self.assertEqual([item.specialist_id for item in plan], ["mapper", "architect"])