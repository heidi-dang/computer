import asyncio
import unittest

from cptr.services.supervisor import Decision, ScopeRecord, ScopeStatus
from cptr.services.supervisor_director import (
    OpenAISupervisorDirector,
    _decision_from_payload,
    _director_request_body,
    _extract_sse_decision,
)


class SupervisorDirectorTests(unittest.TestCase):
    def test_extracts_structured_decision_from_response_text_sse_events(self):
        payload = """event: response.created
data: {"type":"response.created","response":{"id":"resp_test_1"}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"```json\\n{\\n  \\\"scope_satisfied\\\": true,\\n  \\\"goal_satisfied\\\": true\\n}\\n```"}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_test_1"}}
"""

        response_id, decision = _extract_sse_decision(payload)

        self.assertEqual(response_id, "resp_test_1")
        self.assertEqual(decision, {"scope_satisfied": True, "goal_satisfied": True})

    def test_extracts_structured_decision_from_terminal_output_text_event(self):
        payload = """event: response.created
data: {"type":"response.created","response":{"id":"resp_test_done"}}

event: response.output_text.done
data: {"type":"response.output_text.done","text":"{\\"decision\\":\\"PASS\\"}"}

event: response.completed
data: {"type":"response.completed","response":{"id":"resp_test_done"}}
"""

        response_id, decision = _extract_sse_decision(payload)

        self.assertEqual(response_id, "resp_test_done")
        self.assertEqual(decision, {"decision": "PASS"})

    def test_extracts_structured_decision_from_terminal_output_item_event(self):
        payload = """data: {"type":"response.output_item.done","item":{"type":"message","content":[{"type":"output_text","text":"{\\"decision\\":\\"FAIL\\",\\"reason\\":\\"checks failed\\"}"}]}}
"""

        response_id, decision = _extract_sse_decision(payload)

        self.assertIsNone(response_id)
        self.assertEqual(
            decision,
            {"decision": "FAIL", "reason": "checks failed"},
        )

    def test_normalizes_provider_compact_decision_without_fabricating_success(self):
        accepted = _decision_from_payload(
            "evaluate", {"decision": "PASS", "reason": "all checks passed"}
        )
        rejected = _decision_from_payload(
            "evaluate", {"decision": "FAIL", "reason": "pytest failed"}
        )

        self.assertTrue(accepted.scope_satisfied)
        self.assertTrue(accepted.goal_satisfied)
        self.assertFalse(rejected.scope_satisfied)
        self.assertIn("pytest failed", rejected.defects)

    def test_uses_provider_compatible_json_prompt_in_streaming_request(self):
        body = _director_request_body(
            model="heidi-opencode-go",
            instructions="return JSON",
            input_text='{"operation":"evaluate"}',
        )

        self.assertTrue(body["stream"])
        self.assertEqual(body["max_output_tokens"], 4096)
        self.assertNotIn("text", body)
        self.assertNotIn("previous_response_id", body)
        self.assertIn("JSON", body["instructions"])

    def test_reuses_concrete_diagnosis_without_a_redundant_planning_call(self):
        director = OpenAISupervisorDirector.__new__(OpenAISupervisorDirector)
        diagnosis = Decision(
            defects=["gate failed"],
            next_action_required=True,
            next_assignment="Repair the gate and retry",
        )

        planned = asyncio.run(
            director.plan_next_action(
                monitor=object(),
                scope=object(),
                decision=diagnosis,
            )
        )

        self.assertIs(planned, diagnosis)

    def test_serializes_scope_status_enum_as_its_value(self):
        from cptr.services.supervisor_director import _json_safe

        self.assertEqual(_json_safe(ScopeStatus.VERIFYING), "VERIFYING")

    def test_omits_bulky_scope_history_from_director_payload(self):
        from cptr.services.supervisor_director import _json_safe

        scope = ScopeRecord(
            scope_id="scope",
            title="scope",
            description="scope",
            acceptance_criteria=["passes"],
            verification_evidence=[{"raw_output": "large"}],
            failure_evidence=[{"message": "old"}],
        )

        safe = _json_safe(scope)

        self.assertNotIn("verification_evidence", safe)
        self.assertNotIn("failure_evidence", safe)

    def test_omits_raw_worker_stream_from_director_payload(self):
        from cptr.services.supervisor_director import _json_safe

        safe = _json_safe(
            {
                "task": {
                    "status": "COMPLETE",
                    "content": "calculator repaired",
                    "raw_output": [{"type": "reasoning", "content": "large"}],
                }
            }
        )

        self.assertEqual(safe["task"]["content"], "calculator repaired")
        self.assertNotIn("raw_output", safe["task"])


if __name__ == "__main__":
    unittest.main()
