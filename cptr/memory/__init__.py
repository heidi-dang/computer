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
from cptr.memory.workspace import WorkspaceNamespace, resolve_workspace_namespace

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
    "WorkspaceNamespace",
    "get_memory_service",
    "resolve_workspace_namespace",
]
