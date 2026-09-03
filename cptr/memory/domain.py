"""Domain contracts for CPTR's embedded, extraction-ready memory fabric."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ManagedContext:
    rendered: str
    items: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class PrepareContextInput:
    user_id: str
    workspace: str = ""
    task_key: str = ""
    current_message: str = ""
    recent_messages: list[dict[str, Any]] = field(default_factory=list)
    mentioned_files: list[str] = field(default_factory=list)
    max_chars: int | None = None
    runtime_request: Any | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class MemoryContextBundle:
    context_id: str
    status: str
    memory_version: int
    rendered: str
    items: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_id: str | None = None
    candidate_count: int = 0
    compiled_chars: int = 0


@dataclass(frozen=True)
class CheckpointState:
    user_id: str
    workspace: str
    task_key: str
    stage: str
    state: dict[str, Any] = field(default_factory=dict)
    memory_version: int | None = None


@dataclass(frozen=True)
class Checkpoint:
    checkpoint_id: str
    version: int
    stage: str
    memory_version: int
    created_at_ms: int


@dataclass(frozen=True)
class ConsolidationInput:
    user_id: str
    workspace: str
    scope: str
    text: str
    heading: str = ""
    kind: str | None = None
    structured_value: dict[str, Any] = field(default_factory=dict)
    source_event_ids: list[str] = field(default_factory=list)
    trust_level: str = "agent_observation"
    confidence: float = 0.85
    importance: float = 0.5
    valid_from_ms: int | None = None
    verification_ttl_seconds: int | None = None
    branch_id: str | None = None


@dataclass(frozen=True)
class MemoryReplacement:
    user_id: str
    workspace: str
    scope: str
    canonical_text: str
    kind: str = "semantic"
    structured_value: dict[str, Any] = field(default_factory=dict)
    source_event_ids: list[str] = field(default_factory=list)
    trust_level: str = "agent_observation"
    confidence: float = 0.9
    importance: float = 0.5
    valid_from_ms: int | None = None
    verification_ttl_seconds: int | None = None
    branch_id: str | None = None


@dataclass(frozen=True)
class MemoryQuery:
    user_id: str
    workspace: str
    query: str
    scope: str = "both"
    kinds: tuple[str, ...] = ()
    include_historical: bool = False
    branch_id: str | None = None
    limit: int = 12
    now_ms: int | None = None


@dataclass(frozen=True)
class MemoryResult:
    memory_id: str
    scope: str
    kind: str
    canonical_text: str
    score: float
    reason: str
    confidence: float
    importance: float
    trust_level: str
    status: str
    verification_stale: bool
    valid_from_ms: int | None
    valid_until_ms: int | None
    branch_id: str | None


@dataclass(frozen=True)
class VerificationResult:
    memory_id: str
    verified_at_ms: int
    verification_expires_at_ms: int | None
    confidence: float


@dataclass(frozen=True)
class MemoryRecordRef:
    memory_id: str
    kind: str
    status: str
    branch_id: str | None = None


@dataclass(frozen=True)
class RetrievalFeedback:
    user_id: str
    workspace: str
    memory_id: str
    context_id: str
    query: str
    rank: int
    score: float
    used: bool
    helpful: bool | None = None
    outcome: str | None = None


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: str
    memory_version: int
    label: str
    created_at_ms: int


@dataclass(frozen=True)
class BranchRef:
    branch_id: str
    name: str
    from_snapshot_id: str | None
    created_at_ms: int
