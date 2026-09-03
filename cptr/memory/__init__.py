"""Embedded CPTR Memory Core public boundary."""

from cptr.memory.domain import (
    BranchRef,
    Checkpoint,
    CheckpointState,
    ConsolidationInput,
    ManagedContext,
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
from cptr.memory.ports import MemoryService
from cptr.memory.service import EmbeddedMemoryService, MemoryUnavailableError, get_memory_service

__all__ = [
    "BranchRef",
    "Checkpoint",
    "CheckpointState",
    "ConsolidationInput",
    "EmbeddedMemoryService",
    "ManagedContext",
    "MemoryContextBundle",
    "MemoryQuery",
    "MemoryRecordRef",
    "MemoryReplacement",
    "MemoryResult",
    "MemoryService",
    "MemoryUnavailableError",
    "PrepareContextInput",
    "RetrievalFeedback",
    "SnapshotRef",
    "VerificationResult",
    "get_memory_service",
]
