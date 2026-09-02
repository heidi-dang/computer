import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_reasoning import (
    FactoryReasoner,
    ProviderReasoningResponse,
    ReasoningBudget,
    ReasoningRequest,
    ReasoningRole,
    ReasoningSchema,
    StructuredReasoningResult,
)
from cptr.services.factory_reasoning_store import SqlFactoryReasoningStore
from cptr.services.factory_store import SqlFactoryStore


class _RecordingProvider:
    provider_name = "recording"

    def __init__(self, responses):
        self.responses = list(responses)
        self.previous_response_ids = []

    async def complete(self, *, request, schema, model_strength, previous_response_id):
        self.previous_response_ids.append(previous_response_id)
        return self.responses.pop(0)


class FactoryReasoningPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.factory_store = SqlFactoryStore(session_factory=self.sessions)
        self.reasoning_store = SqlFactoryReasoningStore(session_factory=self.sessions)
        self.run = await self.factory_store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="durable reasoning",
            acceptance_criteria=["reasoning survives restart"],
            policy={},
            budget={},
            model_id="configured-model",
            idempotency_key="reasoning-persistence-run",
        )
        self.cycle = await self.factory_store.create_cycle(
            self.run.id,
            base_revision="rev-1",
            base_fingerprint="fp-1",
            idempotency_key="reasoning-cycle",
        )
        self.schema = ReasoningSchema(
            schema_id="finding.v1",
            required_fields={"summary": str, "confidence": int},
        )

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_reasoner_restart_reuses_only_same_role_durable_continuation(self):
        first_provider = _RecordingProvider(
            [
                ProviderReasoningResponse(
                    output_text='{"summary":"architect","confidence":90}',
                    provider="recording",
                    model="model-a",
                    response_id="resp-architect-1",
                ),
                ProviderReasoningResponse(
                    output_text='{"summary":"security","confidence":91}',
                    provider="recording",
                    model="model-a",
                    response_id="resp-security-1",
                ),
            ]
        )
        first_reasoner = FactoryReasoner(
            provider=first_provider,
            schemas=[self.schema],
            history_store=self.reasoning_store,
        )
        await first_reasoner.run(self._request(ReasoningRole.ARCHITECT))
        await first_reasoner.run(self._request(ReasoningRole.SECURITY))

        restarted_provider = _RecordingProvider(
            [
                ProviderReasoningResponse(
                    output_text='{"summary":"architect again","confidence":95}',
                    provider="recording",
                    model="model-a",
                    response_id="resp-architect-2",
                )
            ]
        )
        restarted_reasoner = FactoryReasoner(
            provider=restarted_provider,
            schemas=[self.schema],
            history_store=SqlFactoryReasoningStore(session_factory=self.sessions),
        )
        await restarted_reasoner.run(self._request(ReasoningRole.ARCHITECT))

        self.assertEqual(
            restarted_provider.previous_response_ids,
            ["resp-architect-1"],
        )
        self.assertEqual(
            await self.reasoning_store.latest_response_id(
                run_id=self.run.id,
                cycle_id=self.cycle.id,
                role=ReasoningRole.SECURITY,
            ),
            "resp-security-1",
        )

    async def test_reasoning_history_records_only_validated_result_and_safe_metadata(self):
        result = StructuredReasoningResult(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            role=ReasoningRole.DEBUGGER,
            schema_id="finding.v1",
            data={"summary": "root cause", "confidence": 88},
            provider="recording",
            model="configured-model",
            response_id="resp-debugger",
            input_tokens=20,
            output_tokens=10,
            total_tokens=30,
            runtime_ms=50,
            cost_usd=0.012345,
            attempt_count=2,
            provider_metadata={
                "finish_reason": "stop",
                "reasoning_details": "must never persist",
                "raw_output": "must never persist either",
            },
        )

        stored = await self.reasoning_store.record_result(result)
        rows = await self.reasoning_store.list_results(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
        )

        self.assertEqual(rows[0].id, stored.id)
        self.assertEqual(rows[0].data, {"summary": "root cause", "confidence": 88})
        self.assertEqual(rows[0].provider_metadata, {"finish_reason": "stop"})
        self.assertEqual(rows[0].cost_microusd, 12345)
        self.assertNotIn("reasoning_details", str(rows[0].provider_metadata))
        self.assertNotIn("raw_output", str(rows[0].provider_metadata))

    async def test_reasoning_history_is_bounded_and_role_filtered(self):
        for index in range(5):
            await self.reasoning_store.record_result(
                StructuredReasoningResult(
                    run_id=self.run.id,
                    cycle_id=self.cycle.id,
                    role=(
                        ReasoningRole.ARCHITECT
                        if index % 2 == 0
                        else ReasoningRole.RESEARCH
                    ),
                    schema_id="finding.v1",
                    data={"summary": f"result-{index}", "confidence": 80 + index},
                    provider="recording",
                    model="model-a",
                    response_id=f"resp-{index}",
                    input_tokens=1,
                    output_tokens=1,
                    total_tokens=2,
                    runtime_ms=1,
                    cost_usd=0,
                    attempt_count=1,
                    provider_metadata={},
                )
            )

        rows = await self.reasoning_store.list_results(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            role=ReasoningRole.ARCHITECT,
            limit=2,
        )

        self.assertEqual([row.response_id for row in rows], ["resp-4", "resp-2"])

    def _request(self, role: ReasoningRole) -> ReasoningRequest:
        return ReasoningRequest(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            role=role,
            mission="durable reasoning",
            acceptance_criteria=("reasoning survives restart",),
            evidence_ids=("evidence-1",),
            schema_id="finding.v1",
            budget=ReasoningBudget(max_attempts=1),
        )


if __name__ == "__main__":
    unittest.main()
