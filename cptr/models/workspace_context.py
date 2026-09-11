"""Domain models for Workspace Context Snapshots, evidence aggregation, and divergence tracking."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from cptr.utils.redaction import redact_sensitive


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_json_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    except Exception:
        return json.dumps(str(obj), sort_keys=True)


@dataclass
class RepoEvidence:
    """Live Git repository evidence captured from the workspace."""

    is_repo: bool = False
    root: str | None = None
    branch: str | None = None
    revision: str | None = None
    is_dirty: bool = False
    staged_count: int = 0
    unstaged_count: int = 0
    untracked_count: int = 0
    modified_files: list[str] = field(default_factory=list)
    staged_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    recent_commits: list[dict[str, Any]] = field(default_factory=list)
    diff_summary: dict[str, Any] = field(default_factory=dict)
    status: str = "ok"  # ok, degraded, unavailable
    diagnostics: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> RepoEvidence:
        if not data:
            return cls()
        return cls(
            is_repo=bool(data.get("is_repo", False)),
            root=data.get("root"),
            branch=data.get("branch"),
            revision=data.get("revision"),
            is_dirty=bool(data.get("is_dirty", False)),
            staged_count=int(data.get("staged_count", 0)),
            unstaged_count=int(data.get("unstaged_count", 0)),
            untracked_count=int(data.get("untracked_count", 0)),
            modified_files=list(data.get("modified_files") or []),
            staged_files=list(data.get("staged_files") or []),
            untracked_files=list(data.get("untracked_files") or []),
            recent_commits=list(data.get("recent_commits") or []),
            diff_summary=dict(data.get("diff_summary") or {}),
            status=str(data.get("status", "ok")),
            diagnostics=list(data.get("diagnostics") or []),
            error=data.get("error"),
        )


@dataclass
class FdxEvidence:
    """Aggregated FDX intelligence evidence."""

    enabled: bool = False
    status: str = "unavailable"  # ready, degraded, unavailable, disabled
    daemon_running: bool = False
    version: str | None = None
    capabilities: list[str] = field(default_factory=list)
    semantic_status: dict[str, Any] = field(default_factory=dict)
    index_status: dict[str, Any] = field(default_factory=dict)
    impact_summary: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> FdxEvidence:
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            status=str(data.get("status", "unavailable")),
            daemon_running=bool(data.get("daemon_running", False)),
            version=data.get("version"),
            capabilities=list(data.get("capabilities") or []),
            semantic_status=dict(data.get("semantic_status") or {}),
            index_status=dict(data.get("index_status") or {}),
            impact_summary=dict(data.get("impact_summary") or {}),
            metadata=dict(data.get("metadata") or {}),
            diagnostics=list(data.get("diagnostics") or []),
            error=data.get("error"),
        )


@dataclass
class LspEvidence:
    """Aggregated Language Server Protocol (LSP) intelligence evidence."""

    enabled: bool = False
    status: str = "unavailable"  # ready, idle, disabled, unavailable
    active_servers: list[str] = field(default_factory=list)
    diagnostics_count: int = 0
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    symbols_count: int = 0
    symbols: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> LspEvidence:
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", False)),
            status=str(data.get("status", "unavailable")),
            active_servers=list(data.get("active_servers") or []),
            diagnostics_count=int(data.get("diagnostics_count", 0)),
            diagnostics=list(data.get("diagnostics") or []),
            symbols_count=int(data.get("symbols_count", 0)),
            symbols=list(data.get("symbols") or []),
            metadata=dict(data.get("metadata") or {}),
            error=data.get("error"),
        )


@dataclass
class CheckpointDivergence:
    """Divergence measurement against a stale or previous checkpoint baseline."""

    checkpoint_id: str | None = None
    checkpoint_revision: str | None = None
    checkpoint_memory_version: int | None = None
    is_diverged: bool = False
    is_stale: bool = False
    commits_ahead: int = 0
    commits_behind: int = 0
    diverged_files: list[str] = field(default_factory=list)
    memory_drift: int = 0
    divergence_reasons: list[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CheckpointDivergence:
        if not data:
            return cls()
        return cls(
            checkpoint_id=data.get("checkpoint_id"),
            checkpoint_revision=data.get("checkpoint_revision"),
            checkpoint_memory_version=data.get("checkpoint_memory_version"),
            is_diverged=bool(data.get("is_diverged", False)),
            is_stale=bool(data.get("is_stale", False)),
            commits_ahead=int(data.get("commits_ahead", 0)),
            commits_behind=int(data.get("commits_behind", 0)),
            diverged_files=list(data.get("diverged_files") or []),
            memory_drift=int(data.get("memory_drift", 0)),
            divergence_reasons=list(data.get("divergence_reasons") or []),
            summary=str(data.get("summary", "")),
        )


@dataclass
class MemoryEvidence:
    """Optional memory inputs including canonical procedures, profiles, and snippets."""

    enabled: bool = True
    status: str = "ok"  # ok, disabled, degraded
    memory_version: int | None = None
    canonical_memories: list[str] = field(default_factory=list)
    managed_context: str | None = None
    snippets: list[dict[str, Any]] = field(default_factory=list)
    raw_bundle: dict[str, Any] = field(default_factory=dict)
    diagnostics: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> MemoryEvidence:
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            status=str(data.get("status", "ok")),
            memory_version=data.get("memory_version"),
            canonical_memories=list(data.get("canonical_memories") or []),
            managed_context=data.get("managed_context"),
            snippets=list(data.get("snippets") or []),
            raw_bundle=dict(data.get("raw_bundle") or {}),
            diagnostics=list(data.get("diagnostics") or []),
            error=data.get("error"),
        )


@dataclass
class EnvironmentEvidence:
    """Host and runtime environment context, with sensitive values strictly redacted."""

    hostname: str | None = None
    os_info: str | None = None
    architecture: str | None = None
    shell: str | None = None
    python_version: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    tools: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> EnvironmentEvidence:
        if not data:
            return cls()
        return cls(
            hostname=data.get("hostname"),
            os_info=data.get("os_info"),
            architecture=data.get("architecture"),
            shell=data.get("shell"),
            python_version=data.get("python_version"),
            env_vars=dict(data.get("env_vars") or {}),
            tools=list(data.get("tools") or []),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class BoundedWorkersEvidence:
    """Bounded snapshot of active DirectCodingWorkers for the workspace.

    Worker worktree paths and branch names are redacted in the serialized
    external representation; only safe identity fields are preserved.
    """

    enabled: bool = True
    status: str = "ok"  # ok, disabled, degraded, unavailable
    total_count: int = 0
    active_count: int = 0
    workers: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BoundedWorkersEvidence":
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            status=str(data.get("status", "ok")),
            total_count=int(data.get("total_count", 0)),
            active_count=int(data.get("active_count", 0)),
            workers=list(data.get("workers") or []),
            diagnostics=list(data.get("diagnostics") or []),
            error=data.get("error"),
        )


@dataclass
class ProjectEvidence:
    """Discovered project structure: manifests, test files, and scripts.

    Discovery is bounded and path-safe; paths are workspace-relative.
    Partial failures degrade to explicit diagnostics rather than raising.
    """

    enabled: bool = True
    status: str = "ok"  # ok, degraded, unavailable
    detected_language: str | None = None
    manifest_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    script_files: list[str] = field(default_factory=list)
    source_roots: list[str] = field(default_factory=list)
    file_count: int = 0
    diagnostics: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProjectEvidence":
        if not data:
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            status=str(data.get("status", "ok")),
            detected_language=data.get("detected_language"),
            manifest_files=list(data.get("manifest_files") or []),
            test_files=list(data.get("test_files") or []),
            script_files=list(data.get("script_files") or []),
            source_roots=list(data.get("source_roots") or []),
            file_count=int(data.get("file_count", 0)),
            diagnostics=list(data.get("diagnostics") or []),
            error=data.get("error"),
        )


@dataclass
class InstructionContext:
    """Optional instruction inputs directing agent or compiler execution."""

    system_instructions: str | None = None
    user_instructions: str | None = None
    steering_instructions: list[str] = field(default_factory=list)
    task_instructions: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> InstructionContext:
        if not data:
            return cls()
        return cls(
            system_instructions=data.get("system_instructions"),
            user_instructions=data.get("user_instructions"),
            steering_instructions=list(data.get("steering_instructions") or []),
            task_instructions=data.get("task_instructions"),
        )


@dataclass
class WorkspaceContextSnapshot:
    """Comprehensive snapshot of workspace, repo evidence, intelligence, memory, and divergence."""

    workspace_root: str
    snapshot_id: str = field(default_factory=lambda: f"wcs_{uuid.uuid4().hex}")
    version: int = 1
    user_id: str | None = None
    workspace_id: str | None = None
    active_workspace_id: str | None = None
    created_at_ms: int = field(default_factory=_now_ms)
    repo: RepoEvidence = field(default_factory=RepoEvidence)
    fdx: FdxEvidence = field(default_factory=FdxEvidence)
    lsp: LspEvidence = field(default_factory=LspEvidence)
    divergence: CheckpointDivergence = field(default_factory=CheckpointDivergence)
    memory: MemoryEvidence = field(default_factory=MemoryEvidence)
    environment: EnvironmentEvidence = field(default_factory=EnvironmentEvidence)
    instructions: InstructionContext = field(default_factory=InstructionContext)
    workers: BoundedWorkersEvidence = field(default_factory=BoundedWorkersEvidence)
    project: ProjectEvidence = field(default_factory=ProjectEvidence)
    diagnostics: list[str] = field(default_factory=list)
    content_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content_digest:
            self.content_digest = self.compute_digest()

    @property
    def revision(self) -> str | None:
        return self.repo.revision

    @property
    def is_dirty(self) -> bool:
        return self.repo.is_dirty

    @property
    def fdx_status(self) -> str:
        return self.fdx.status

    def compute_digest(self) -> str:
        """Compute stable SHA-256 digest of canonical context-bearing state."""
        canonical_state = {
            "version": self.version,
            "workspace_root": self.workspace_root,
            "workspace_id": self.workspace_id,
            "active_workspace_id": self.active_workspace_id,
            "repo": {
                "revision": self.repo.revision,
                "is_dirty": self.repo.is_dirty,
                "branch": self.repo.branch,
                "staged_count": self.repo.staged_count,
                "unstaged_count": self.repo.unstaged_count,
                "untracked_count": self.repo.untracked_count,
            },
            "fdx": {
                "status": self.fdx.status,
                "semantic_status": self.fdx.semantic_status,
                "index_status": self.fdx.index_status,
            },
            "lsp": {
                "status": self.lsp.status,
                "active_servers": self.lsp.active_servers,
                "diagnostics_count": self.lsp.diagnostics_count,
                "symbols_count": self.lsp.symbols_count,
            },
            "divergence": {
                "is_diverged": self.divergence.is_diverged,
                "is_stale": self.divergence.is_stale,
                "checkpoint_id": self.divergence.checkpoint_id,
                "checkpoint_revision": self.divergence.checkpoint_revision,
                "checkpoint_memory_version": self.divergence.checkpoint_memory_version,
                "commits_ahead": self.divergence.commits_ahead,
                "commits_behind": self.divergence.commits_behind,
                "memory_drift": self.divergence.memory_drift,
                "divergence_reasons": self.divergence.divergence_reasons,
            },
            "memory": {
                "memory_version": self.memory.memory_version,
                "canonical_memories": self.memory.canonical_memories,
                "managed_context": self.memory.managed_context,
                "snippets": self.memory.snippets,
            },
            "environment": self.environment.to_dict(),
            "instructions": self.instructions.to_dict(),
        }
        encoded = _safe_json_dumps(canonical_state).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        """Convert snapshot to safely redacted JSON-serializable dictionary."""
        raw = {
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "workspace_root": self.workspace_root,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "active_workspace_id": self.active_workspace_id,
            "created_at_ms": self.created_at_ms,
            "repo": self.repo.to_dict(),
            "fdx": self.fdx.to_dict(),
            "lsp": self.lsp.to_dict(),
            "divergence": self.divergence.to_dict(),
            "memory": self.memory.to_dict(),
            "environment": self.environment.to_dict(),
            "instructions": self.instructions.to_dict(),
            "workers": self.workers.to_dict(),
            "project": self.project.to_dict(),
            "diagnostics": list(self.diagnostics),
            "content_digest": self.content_digest,
            "metadata": dict(self.metadata),
        }
        return redact_sensitive(raw)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkspaceContextSnapshot:
        """Instantiate snapshot from dictionary."""
        snapshot = cls(
            snapshot_id=str(data.get("snapshot_id") or f"wcs_{uuid.uuid4().hex}"),
            version=int(data.get("version") or 1),
            workspace_root=str(data.get("workspace_root") or ""),
            user_id=data.get("user_id"),
            workspace_id=data.get("workspace_id"),
            active_workspace_id=data.get("active_workspace_id"),
            created_at_ms=int(data.get("created_at_ms") or _now_ms()),
            repo=RepoEvidence.from_dict(data.get("repo")),
            fdx=FdxEvidence.from_dict(data.get("fdx")),
            lsp=LspEvidence.from_dict(data.get("lsp")),
            divergence=CheckpointDivergence.from_dict(data.get("divergence")),
            memory=MemoryEvidence.from_dict(data.get("memory")),
            environment=EnvironmentEvidence.from_dict(data.get("environment")),
            instructions=InstructionContext.from_dict(data.get("instructions")),
            workers=BoundedWorkersEvidence.from_dict(data.get("workers")),
            project=ProjectEvidence.from_dict(data.get("project")),
            diagnostics=list(data.get("diagnostics") or []),
            content_digest=str(data.get("content_digest") or ""),
            metadata=dict(data.get("metadata") or {}),
        )
        if not snapshot.content_digest:
            snapshot.content_digest = snapshot.compute_digest()
        return snapshot
