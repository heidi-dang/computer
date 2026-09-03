"""Ports for CPTR memory. Callers depend on these contracts, not storage internals."""

from __future__ import annotations

from typing import Protocol

from cptr.memory.domain import (
    BranchRef,
    Checkpoint,
    CheckpointState,
    ConsolidationInput,
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
