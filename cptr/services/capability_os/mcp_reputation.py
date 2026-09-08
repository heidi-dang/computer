"""Server-derived MCP reliability reputation from trusted execution evidence."""

from __future__ import annotations

from dataclasses import dataclass

from cptr.services.capability_os.contracts import ArtifactKind
from cptr.services.capability_os.mcp_fabric import beta_lower_bound
from cptr.services.capability_os.store import SqlCapabilityOsStore


@dataclass(frozen=True)
class McpReputationSnapshot:
    server_id: str
    successes: int
    failures: int
    last_observed_at_ms: int | None

    @property
    def total(self) -> int:
        return self.successes + self.failures

    @property
    def reliability_lower_bound(self) -> float:
        return beta_lower_bound(successes=self.successes, failures=self.failures)

    def to_api(self) -> dict[str, int | float | str | None]:
        return {
            "serverId": self.server_id,
            "successes": self.successes,
            "failures": self.failures,
            "total": self.total,
            "reliabilityLowerBound": self.reliability_lower_bound,
            "lastObservedAtMs": self.last_observed_at_ms,
        }


class McpReputationService:
    """Aggregate exact sufficient statistics from server-produced MCP outcomes."""

    def __init__(self, *, store: SqlCapabilityOsStore) -> None:
        self._store = store

    async def snapshot(self, *, user_id: str, server_id: str) -> McpReputationSnapshot:
        normalized_server_id = str(server_id).strip()
        if not normalized_server_id:
            raise ValueError("MCP reputation server_id must not be blank")
        artifacts = await self._store.list_artifacts(
            user_id=user_id,
            include_global=False,
            kinds=(ArtifactKind.MCP_ADAPTER.value,),
            limit=1_000,
        )
        matching = tuple(
            row.content_digest
            for row in artifacts
            if str((row.spec or {}).get("serverId") or "").strip() == normalized_server_id
        )
        evidence = await self._store.list_evidence_for_artifacts(
            matching,
            kinds=("mcp.invoke.outcome",),
            producer_identity="capability-os-control",
            limit=10_000,
        )
        successes = 0
        failures = 0
        last_observed_at_ms: int | None = None
        for row in evidence:
            claims = dict(row.claims or {})
            if str(claims.get("serverId") or "").strip() != normalized_server_id:
                continue
            succeeded = claims.get("success")
            if succeeded is True:
                successes += 1
            elif succeeded is False:
                failures += 1
            else:
                continue
            last_observed_at_ms = max(
                int(row.created_at_ms),
                last_observed_at_ms or int(row.created_at_ms),
            )
        return McpReputationSnapshot(
            server_id=normalized_server_id,
            successes=successes,
            failures=failures,
            last_observed_at_ms=last_observed_at_ms,
        )
