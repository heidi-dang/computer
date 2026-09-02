"""Bounded state-specific inputs and outputs for Dark Factory phases."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from cptr.models import FactoryCycle, FactoryEvidence, FactoryGateResult, FactoryRun
from cptr.services.factory_domain import FactoryState
from cptr.services.factory_gates import EvidenceAuthority, FactoryGateCategory, FactoryGateStatus
from cptr.services.factory_victory import FactoryVictoryDecision
from cptr.utils.redaction import redact_sensitive

_MAX_PHASE_PAYLOAD_BYTES = 64 * 1024
_MAX_PHASE_ARTIFACTS = 50
_MAX_PHASE_GATES = 50


class PhaseFailureCategory(str, Enum):
    IMPLEMENTATION = "implementation"
    TEST = "test"
    ENVIRONMENT = "environment"
    CAPABILITY = "capability"
    SECURITY = "security"
    CI = "ci"
    RUNTIME = "runtime"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PhaseFailure:
    category: PhaseFailureCategory
    code: str
    summary: str
    gate_id: str | None = None

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("phase failure code must not be blank")
        if not self.summary.strip():
            raise ValueError("phase failure summary must not be blank")
        if len(self.code) > 160 or len(self.summary) > 4_000:
            raise ValueError("phase failure fields exceed bounded length")

    @property
    def signature(self) -> str:
        # The signature intentionally excludes free-form paths, line numbers,
        # timestamps and provider prose. Stable classification identity drives
        # retry escalation; the full summary remains durable evidence.
        payload = {
            "category": self.category.value,
            "code": self.code.strip().upper(),
            "gate_id": (self.gate_id or "").strip().lower(),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class PhaseArtifact:
    key: str
    kind: str
    source: str
    authority: EvidenceAuthority
    payload: dict[str, Any]
    gate_id: str | None = None
    revision: str | None = None
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.key.strip() or not self.kind.strip() or not self.source.strip():
            raise ValueError("phase artifact key/kind/source must not be blank")
        safe = redact_sensitive(self.payload)
        raw = json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        if len(raw) > _MAX_PHASE_PAYLOAD_BYTES:
            raise ValueError("phase artifact payload exceeds bounded size")


@dataclass(frozen=True)
class PhaseGateUpdate:
    gate_id: str
    category: FactoryGateCategory
    required: bool
    applicable: bool
    status: FactoryGateStatus
    artifact_keys: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    evaluated_revision: str | None = None
    evaluated_fingerprint: str | None = None
    reason: str | None = None
    attempt: int = 1

    def __post_init__(self) -> None:
        if not self.gate_id.strip():
            raise ValueError("phase gate id must not be blank")
        if self.attempt <= 0:
            raise ValueError("phase gate attempt must be positive")
        if len(self.artifact_keys) > _MAX_PHASE_ARTIFACTS or len(self.evidence_ids) > 100:
            raise ValueError("phase gate evidence references exceed bounded count")


@dataclass(frozen=True)
class PhaseOutcome:
    reason: str
    next_state: FactoryState | None = None
    artifacts: tuple[PhaseArtifact, ...] = ()
    gates: tuple[PhaseGateUpdate, ...] = ()
    cycle_updates: dict[str, Any] = field(default_factory=dict)
    run_next_action: str | None = None
    target_revision: str | None = None
    target_fingerprint: str | None = None
    failure: PhaseFailure | None = None
    victory_decision: FactoryVictoryDecision | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("phase outcome reason must not be blank")
        if len(self.artifacts) > _MAX_PHASE_ARTIFACTS:
            raise ValueError("phase outcome has too many artifacts")
        if len(self.gates) > _MAX_PHASE_GATES:
            raise ValueError("phase outcome has too many gate updates")
        keys = [artifact.key for artifact in self.artifacts]
        if len(keys) != len(set(keys)):
            raise ValueError("phase artifact keys must be unique")
        if (self.target_revision is None) != (self.target_fingerprint is None):
            raise ValueError("phase target revision and fingerprint must be supplied together")
        raw = json.dumps(
            redact_sensitive(self.cycle_updates),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(raw) > _MAX_PHASE_PAYLOAD_BYTES:
            raise ValueError("phase cycle updates exceed bounded size")


@dataclass(frozen=True)
class PhaseContext:
    run: FactoryRun
    cycle: FactoryCycle
    evidence: tuple[FactoryEvidence, ...]
    gates: tuple[FactoryGateResult, ...]


class PhaseHandler(Protocol):
    async def execute(self, context: PhaseContext) -> PhaseOutcome: ...
