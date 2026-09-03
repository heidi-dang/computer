"""Ports for CPTR memory. Callers depend on these contracts, not storage internals."""

from __future__ import annotations

from typing import Protocol

from cptr.memory.domain import (
    BranchRef,
    Checkpoint,
    CheckpointState,
    ConsolidationInput,
    MemoryConflictRef,
    MemoryContextBundle,
    MemoryQuery,
    MemoryRecordRef,
    MemoryReplacement,
    MemoryResult,
    PrepareContextInput,
    RetrievalFeedback,
    SnapshotRef,
    VerificationResult,
)


class MemoryService(Protocol):
    async def prepare_context(self, value: PrepareContextInput) -> MemoryContextBundle: ...

    async def record_event(self, **event) -> str: ...

    async def checkpoint(self, value: CheckpointState) -> Checkpoint: ...

    async def queue_consolidation(self, **value) -> str: ...

    async def consolidate(self, value: ConsolidationInput) -> MemoryRecordRef: ...

    async def search(self, value: MemoryQuery) -> list[MemoryResult]: ...

    async def verify(
        self, memory_id: str, *, user_id: str, workspace: str
    ) -> VerificationResult: ...

    async def supersede(
        self, old_memory_id: str, replacement: MemoryReplacement
    ) -> MemoryRecordRef: ...

    async def feedback(self, value: RetrievalFeedback) -> None: ...

    async def forget(
        self,
        memory_id: str,
        *,
        user_id: str,
        workspace: str,
        source_forgetter=None,
    ) -> bool: ...

    async def rebuild_derived_indexes(
        self, user_id: str, workspace: str, *, batch_size: int = 200
    ) -> dict: ...

    async def queue_rebuild(self, user_id: str, workspace: str) -> str: ...

    async def inspect(self, memory_id: str, *, user_id: str, workspace: str) -> dict: ...

    async def index_memory(self, memory_id: str, *, user_id: str, workspace: str) -> None: ...

    async def analyze_conflicts(self, memory_id: str) -> list[MemoryConflictRef]: ...

    async def list_conflicts(
        self,
        *,
        user_id: str,
        workspace: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]: ...

    async def resolve_conflict(self, conflict_id: str, *, resolution: str) -> dict: ...

    async def time_travel(
        self,
        user_id: str,
        workspace: str,
        *,
        at_ms: int,
        known_at_ms: int | None = None,
        limit: int = 1000,
    ) -> list[dict]: ...

    async def compare_snapshots(
        self,
        user_id: str,
        workspace: str,
        left_snapshot_id: str,
        right_snapshot_id: str,
    ) -> dict: ...

    async def merge_branch(
        self,
        user_id: str,
        workspace: str,
        branch_id: str,
        *,
        strategy: str = "verified_only",
    ) -> dict: ...

    async def health(self, *, user_id: str, workspace: str) -> dict: ...

    async def snapshot(self, user_id: str, workspace: str, *, label: str = "") -> SnapshotRef: ...

    async def create_branch(
        self,
        user_id: str,
        workspace: str,
        *,
        name: str,
        from_snapshot_id: str | None = None,
    ) -> BranchRef: ...

    async def restore_snapshot(
        self, user_id: str, workspace: str, snapshot_id: str
    ) -> SnapshotRef: ...
