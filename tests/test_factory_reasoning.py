import json
import unittest

import httpx

from cptr.services.factory_domain import FactoryState
from cptr.services.factory_reasoning import (
    FactoryReasoner,
    ModelStrength,
    OpenAIResponsesReasoningProvider,
    ProviderReasoningResponse,
    ReasoningBudget,
    ReasoningBudgetExceeded,
    ReasoningProviderError,
    ReasoningRequest,
    ReasoningRole,
    ReasoningSchema,
    StructuredReasoningError,
    model_strength_for_role,
    reasoning_roles_for_state,
)


class _ScriptedProvider:
    provider_name = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, *, request, schema, model_strength, previous_response_id):
        self.calls.append(
            {
                "role": request.role,
                "model_strength": model_strength,
                "previous_response_id": previous_response_id,
            }
        )
        if not self.responses:
            raise AssertionError("provider called more times than scripted")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FactoryReasoningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.schema = ReasoningSchema(
            schema_id="finding.v1",
            required_fields={"summary": str, "confidence": int},
        )

    async def test_continuation_is_isolated_by_run_cycle_and_role(self):
        provider = _ScriptedProvider(
            [
                ProviderReasoningResponse(
                    output_text='{"summary":"architect-a","confidence":90}',
                    provider="scripted",
                    model="strong-model",
                    response_id="resp-architect-1",
                ),
                ProviderReasoningResponse(
                    output_text='{"summary":"security-a","confidence":80}',
                    provider="scripted",
                    model="strong-model",
                    response_id="resp-security-1",
                ),
                ProviderReasoningResponse(
                    output_text='{"summary":"architect-b","confidence":95}',
                    provider="scripted",
                    model="strong-model",
                    response_id="resp-architect-2",
                ),
            ]
        )
        reasoner = FactoryReasoner(provider=provider, schemas=[self.schema])

        await reasoner.run(self._request(role=ReasoningRole.ARCHITECT))
        await reasoner.run(self._request(role=ReasoningRole.SECURITY))
        await reasoner.run(self._request(role=ReasoningRole.ARCHITECT))

        self.assertIsNone(provider.calls[0]["previous_response_id"])
        self.assertIsNone(provider.calls[1]["previous_response_id"])
        self.assertEqual(provider.calls[2]["previous_response_id"], "resp-architect-1")

    async def test_invalid_json_and_schema_are_retried_only_within_budget(self):
        provider = _ScriptedProvider(
            [
                ProviderReasoningResponse(
                    output_text="not-json",
                    provider="scripted",
                    model="standard-model",
                    response_id="bad-json",
                ),
                ProviderReasoningResponse(
                    output_text='{"summary":"missing confidence"}',
                    provider="scripted",
                    model="standard-model",
                    response_id="bad-schema",
                ),
            ]
        )
        reasoner = FactoryReasoner(provider=provider, schemas=[self.schema])

        with self.assertRaisesRegex(StructuredReasoningError, "confidence"):
            await reasoner.run(
                self._request(
                    role=ReasoningRole.RESEARCH,
                    budget=ReasoningBudget(max_attempts=2),
                )
            )

        self.assertEqual(len(provider.calls), 2)
        self.assertIsNone(provider.calls[0]["previous_response_id"])
        self.assertIsNone(provider.calls[1]["previous_response_id"])

    async def test_transient_provider_failure_retries_within_attempt_budget(self):
        provider = _ScriptedProvider(
            [
                ReasoningProviderError("temporary provider failure"),
                ProviderReasoningResponse(
                    output_text='{"summary":"recovered","confidence":91}',
                    provider="scripted",
                    model="standard-model",
                    response_id="resp-recovered",
                ),
            ]
        )
        reasoner = FactoryReasoner(provider=provider, schemas=[self.schema])

        result = await reasoner.run(
            self._request(
                role=ReasoningRole.RESEARCH,
                budget=ReasoningBudget(max_attempts=2),
            )
        )

        self.assertEqual(result.attempt_count, 2)
        self.assertEqual(len(provider.calls), 2)
        self.assertIsNone(provider.calls[1]["previous_response_id"])

    async def test_provider_failure_stops_at_attempt_budget(self):
        provider = _ScriptedProvider(
            [
                ReasoningProviderError("provider failure one"),
                ReasoningProviderError("provider failure two"),
            ]
        )
        reasoner = FactoryReasoner(provider=provider, schemas=[self.schema])

        with self.assertRaisesRegex(ReasoningProviderError, "failure two"):
            await reasoner.run(
                self._request(
                    role=ReasoningRole.RESEARCH,
                    budget=ReasoningBudget(max_attempts=2),
                )
            )

        self.assertEqual(len(provider.calls), 2)

    async def test_budget_exhaustion_prevents_an_additional_provider_call(self):
        provider = _ScriptedProvider(
            [
                ProviderReasoningResponse(
                    output_text="not-json",
                    provider="scripted",
                    model="standard-model",
                    response_id="bad-json",
                    input_tokens=40,
                    output_tokens=20,
                    cost_usd=0.25,
                ),
                ProviderReasoningResponse(
                    output_text='{"summary":"would-pass","confidence":99}',
                    provider="scripted",
                    model="standard-model",
                    response_id="should-not-run",
                ),
            ]
        )
        reasoner = FactoryReasoner(provider=provider, schemas=[self.schema])

        with self.assertRaisesRegex(ReasoningBudgetExceeded, "token"):
            await reasoner.run(
                self._request(
                    role=ReasoningRole.RESEARCH,
                    budget=ReasoningBudget(max_attempts=3, max_total_tokens=50),
                )
            )

        self.assertEqual(len(provider.calls), 1)

    async def test_result_exposes_only_structured_output_and_safe_provider_metadata(self):
        provider = _ScriptedProvider(
            [
                ProviderReasoningResponse(
                    output_text='{"summary":"root cause","confidence":88}',
                    provider="scripted",
                    model="strong-model",
                    response_id="resp-safe",
                    input_tokens=12,
                    output_tokens=8,
                    cost_usd=0.01,
                    provider_metadata={
                        "finish_reason": "stop",
                        "reasoning_details": "private chain of thought",
                        "raw_output": "provider transcript",
                    },
                )
            ]
        )
        reasoner = FactoryReasoner(provider=provider, schemas=[self.schema])

        result = await reasoner.run(self._request(role=ReasoningRole.DEBUGGER))
        payload = result.to_dict()

        self.assertEqual(result.data, {"summary": "root cause", "confidence": 88})
        self.assertEqual(payload["provider_metadata"], {"finish_reason": "stop"})
        self.assertNotIn("output_text", payload)
        self.assertNotIn("reasoning_details", str(payload))
        self.assertNotIn("provider transcript", str(payload))

    async def test_successful_result_tracks_usage_runtime_and_attempt_count(self):
        provider = _ScriptedProvider(
            [
                ProviderReasoningResponse(
                    output_text='{"summary":"verified","confidence":100}',
                    provider="scripted",
                    model="strong-model",
                    response_id="resp-verified",
                    input_tokens=25,
                    output_tokens=10,
                    cost_usd=0.02,
                )
            ]
        )
        reasoner = FactoryReasoner(provider=provider, schemas=[self.schema])

        result = await reasoner.run(self._request(role=ReasoningRole.VERIFIER))

        self.assertEqual(result.attempt_count, 1)
        self.assertEqual(result.input_tokens, 25)
        self.assertEqual(result.output_tokens, 10)
        self.assertEqual(result.total_tokens, 35)
        self.assertEqual(result.cost_usd, 0.02)
        self.assertGreaterEqual(result.runtime_ms, 0)
        self.assertEqual(result.response_id, "resp-verified")

    async def test_openai_responses_adapter_uses_configured_strength_and_continuation(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "resp-openai",
                    "model": body["model"],
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"summary":"secure","confidence":97}',
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 31,
                        "output_tokens": 11,
                        "total_tokens": 42,
                    },
                },
            )

        provider = OpenAIResponsesReasoningProvider(
            api_key="test-key",
            standard_model="configured-standard",
            strongest_model="configured-strongest",
            base_url="https://reasoning.example/v1",
            transport=httpx.MockTransport(handler),
        )
        request = self._request(
            role=ReasoningRole.SECURITY,
            budget=ReasoningBudget(max_attempts=1, max_output_tokens=777),
        )

        response = await provider.complete(
            request=request,
            schema=self.schema,
            model_strength=ModelStrength.STRONGEST,
            previous_response_id="resp-prior-security",
        )

        self.assertEqual(response.output_text, '{"summary":"secure","confidence":97}')
        self.assertEqual(response.input_tokens, 31)
        self.assertEqual(response.output_tokens, 11)
        self.assertEqual(response.model, "configured-strongest")
        self.assertEqual(requests[0]["model"], "configured-strongest")
        self.assertEqual(requests[0]["previous_response_id"], "resp-prior-security")
        self.assertEqual(requests[0]["max_output_tokens"], 777)
        self.assertNotIn("reasoning", requests[0])
        self.assertIn("Return only one JSON object", requests[0]["instructions"])
        self.assertIn('"summary":"string"', requests[0]["instructions"])
        self.assertIn('"confidence":"integer"', requests[0]["instructions"])

    async def test_openai_responses_adapter_omits_continuation_on_first_role_call(self):
        requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(
                200,
                json={
                    "id": "resp-first",
                    "model": body["model"],
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": '{"summary":"research","confidence":75}',
                                }
                            ],
                        }
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
                },
            )

        provider = OpenAIResponsesReasoningProvider(
            api_key="test-key",
            standard_model="configured-standard",
            strongest_model="configured-strongest",
            transport=httpx.MockTransport(handler),
        )
        await provider.complete(
            request=self._request(role=ReasoningRole.RESEARCH),
            schema=self.schema,
            model_strength=ModelStrength.STANDARD,
            previous_response_id=None,
        )

        self.assertEqual(requests[0]["model"], "configured-standard")
        self.assertNotIn("previous_response_id", requests[0])

    def test_adaptive_role_policy_skips_reasoning_for_deterministic_phases(self):
        self.assertEqual(
            reasoning_roles_for_state(FactoryState.BASELINING, deterministic=True),
            (),
        )
        self.assertEqual(
            reasoning_roles_for_state(FactoryState.SECURITY_REVIEW),
            (ReasoningRole.SECURITY,),
        )
        self.assertEqual(
            reasoning_roles_for_state(FactoryState.ADVERSARIAL_REVIEW),
            (ReasoningRole.ADVERSARIAL,),
        )
        self.assertEqual(
            reasoning_roles_for_state(FactoryState.VICTORY_JUDGING),
            (ReasoningRole.VICTORY_JUDGE,),
        )

    def test_high_risk_roles_require_strongest_configured_model_policy(self):
        for role in (
            ReasoningRole.SECURITY,
            ReasoningRole.ADVERSARIAL,
            ReasoningRole.VICTORY_JUDGE,
        ):
            with self.subTest(role=role):
                self.assertEqual(model_strength_for_role(role), ModelStrength.STRONGEST)
        self.assertEqual(
            model_strength_for_role(ReasoningRole.RESEARCH),
            ModelStrength.STANDARD,
        )

    def _request(
        self,
        *,
        role: ReasoningRole,
        budget: ReasoningBudget | None = None,
    ) -> ReasoningRequest:
        return ReasoningRequest(
            run_id="factory-1",
            cycle_id="cycle-1",
            role=role,
            mission="repair the selected defect",
            acceptance_criteria=("all required checks pass",),
            evidence_ids=("evidence-1",),
            schema_id="finding.v1",
            budget=budget or ReasoningBudget(max_attempts=2),
        )


if __name__ == "__main__":
    unittest.main()
