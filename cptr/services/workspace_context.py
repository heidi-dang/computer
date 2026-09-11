"""Workspace context snapshot service and compiler interface.

Composes live repository evidence, FDX/LSP intelligence, stale checkpoint
divergence, memory, environment, and instruction inputs into a coherent,
tamper-evident context snapshot.
"""

from __future__ import annotations

import asyncio
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cptr.models.workspace_context import (
    BoundedWorkersEvidence,
    CheckpointDivergence,
    EnvironmentEvidence,
    FdxEvidence,
    InstructionContext,
    LspEvidence,
    MemoryEvidence,
    ProjectEvidence,
    RepoEvidence,
    WorkspaceContextSnapshot,
)
from cptr.utils.redaction import redact_sensitive


def _now_ms() -> int:
    return int(time.time() * 1000)


def _safe_str(val: Any) -> str:
    return str(val or "").strip()


@runtime_checkable
class WorkspaceContextCompilerProtocol(Protocol):
    """Protocol for compiling WorkspaceContextSnapshots into rendered contexts."""

    def compile(
        self,
        snapshot: WorkspaceContextSnapshot,
        *,
        max_chars: int | None = None,
        include_repo: bool = True,
        include_fdx: bool = True,
        include_lsp: bool = True,
        include_divergence: bool = True,
        include_memory: bool = True,
        include_environment: bool = True,
        include_instructions: bool = True,
        include_workers: bool = True,
        include_project: bool = True,
        include_diagnostics: bool = True,
    ) -> str:
        """Compile snapshot into a rendered context string."""
        ...

    def compile_bundle(
        self,
        snapshot: WorkspaceContextSnapshot,
        *,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Compile snapshot into a structured context bundle with metadata."""
        ...


class WorkspaceContextCompiler:
    """Default compiler that produces structured, human- and LLM-readable context."""

    def compile(
        self,
        snapshot: WorkspaceContextSnapshot,
        *,
        max_chars: int | None = None,
        include_repo: bool = True,
        include_fdx: bool = True,
        include_lsp: bool = True,
        include_divergence: bool = True,
        include_memory: bool = True,
        include_environment: bool = True,
        include_instructions: bool = True,
        include_workers: bool = True,
        include_project: bool = True,
        include_diagnostics: bool = True,
    ) -> str:
        sections: list[str] = []

        # 1. Header / Workspace Identification (Preserves Workbench semantics)
        header_lines = [
            f"# Workspace Context Snapshot [{snapshot.snapshot_id}]",
            f"- Version: {snapshot.version}",
            f"- Workspace Root: {snapshot.workspace_root}",
        ]
        if snapshot.workspace_id:
            active_info = (
                f" (Active Target Workspace: {snapshot.active_workspace_id})"
                if snapshot.active_workspace_id
                and snapshot.active_workspace_id != snapshot.workspace_id
                else ""
            )
            header_lines.append(f"- Workspace ID: {snapshot.workspace_id}{active_info}")
        elif snapshot.active_workspace_id:
            header_lines.append(f"- Active Workspace ID: {snapshot.active_workspace_id}")
        if snapshot.user_id:
            header_lines.append(f"- User ID: {snapshot.user_id}")
        header_lines.append(f"- Digest: {snapshot.content_digest}")
        header_lines.append(f"- Captured At: {snapshot.created_at_ms} ms")
        sections.append("\n".join(header_lines))

        # 2. Stale Checkpoint Divergence (Priority warning if diverged or stale)
        if include_divergence and (
            snapshot.divergence.is_diverged
            or snapshot.divergence.is_stale
            or snapshot.divergence.checkpoint_id
        ):
            div = snapshot.divergence
            div_lines = ["## Stale Checkpoint Divergence"]
            if div.is_diverged or div.is_stale:
                div_lines.append("⚠️ **DIVERGENCE DETECTED AGAINST BASELINE CHECKPOINT**")
            else:
                div_lines.append("✅ **Aligned with baseline checkpoint**")
            if div.checkpoint_id:
                div_lines.append(f"- Checkpoint ID: {div.checkpoint_id}")
            if div.checkpoint_revision:
                div_lines.append(f"- Checkpoint Git Revision: {div.checkpoint_revision}")
            if div.checkpoint_memory_version is not None:
                div_lines.append(
                    f"- Checkpoint Memory Version: {div.checkpoint_memory_version} (Drift: {div.memory_drift})"
                )
            if div.commits_ahead or div.commits_behind:
                div_lines.append(
                    f"- Commits: {div.commits_ahead} ahead, {div.commits_behind} behind"
                )
            if div.diverged_files:
                sample_files = div.diverged_files[:10]
                more_suffix = (
                    f" ... (+{len(div.diverged_files) - 10} more)"
                    if len(div.diverged_files) > 10
                    else ""
                )
                div_lines.append(
                    f"- Diverged Files ({len(div.diverged_files)}): {', '.join(sample_files)}{more_suffix}"
                )
            if div.divergence_reasons:
                div_lines.append("- Reasons:")
                for reason in div.divergence_reasons:
                    div_lines.append(f"  * {reason}")
            if div.summary:
                div_lines.append(f"- Summary: {div.summary}")
            sections.append("\n".join(div_lines))

        # 3. Live Repository Evidence
        if include_repo and snapshot.repo.is_repo:
            repo = snapshot.repo
            repo_lines = [
                "## Repository Evidence",
                f"- Git Root: {repo.root or snapshot.workspace_root}",
                f"- Branch: {repo.branch or 'unknown'}",
                f"- HEAD Revision: {repo.revision or 'unknown'}",
                f"- Status: {'DIRTY' if repo.is_dirty else 'CLEAN'} "
                f"({repo.staged_count} staged, {repo.unstaged_count} unstaged, {repo.untracked_count} untracked)",
            ]
            if repo.modified_files:
                sample = repo.modified_files[:8]
                suffix = (
                    f" (+{len(repo.modified_files) - 8} more)"
                    if len(repo.modified_files) > 8
                    else ""
                )
                repo_lines.append(f"- Modified Files: {', '.join(sample)}{suffix}")
            if repo.staged_files:
                sample = repo.staged_files[:8]
                suffix = (
                    f" (+{len(repo.staged_files) - 8} more)" if len(repo.staged_files) > 8 else ""
                )
                repo_lines.append(f"- Staged Files: {', '.join(sample)}{suffix}")
            if repo.untracked_files:
                sample = repo.untracked_files[:8]
                suffix = (
                    f" (+{len(repo.untracked_files) - 8} more)"
                    if len(repo.untracked_files) > 8
                    else ""
                )
                repo_lines.append(f"- Untracked Files: {', '.join(sample)}{suffix}")
            if repo.recent_commits:
                repo_lines.append("- Recent Commits:")
                for c in repo.recent_commits[:5]:
                    sha = str(c.get("sha") or c.get("commit") or "")[:8]
                    msg = str(c.get("message") or c.get("summary") or "").split("\n")[0][:80]
                    repo_lines.append(f"  * {sha} {msg}")
            if repo.diagnostics:
                for d in repo.diagnostics:
                    repo_lines.append(f"- Note: {d}")
            if repo.error:
                repo_lines.append(f"- Note: {repo.error}")
            sections.append("\n".join(repo_lines))
        elif include_repo and not snapshot.repo.is_repo:
            sections.append(
                f"## Repository Evidence\n- Workspace is not a Git repository ({snapshot.repo.error or 'no .git found'})"
            )

        # 4. FDX Intelligence Aggregation
        if include_fdx and (snapshot.fdx.enabled or snapshot.fdx.status != "unavailable"):
            fdx = snapshot.fdx
            fdx_lines = [
                "## FDX Intelligence",
                f"- Status: {fdx.status.upper()} (daemon: {'running' if fdx.daemon_running else 'stopped'})",
            ]
            if fdx.version:
                fdx_lines.append(f"- Version: {fdx.version}")
            if fdx.capabilities:
                fdx_lines.append(f"- Capabilities: {', '.join(fdx.capabilities)}")
            if fdx.semantic_status:
                fdx_lines.append(f"- Semantic Status: {fdx.semantic_status}")
            if fdx.index_status:
                fdx_lines.append(f"- Index Status: {fdx.index_status}")
            if fdx.impact_summary:
                fdx_lines.append(f"- Impact Summary: {fdx.impact_summary}")
            if fdx.diagnostics:
                for d in fdx.diagnostics:
                    fdx_lines.append(f"- Diagnostic: {d}")
            if fdx.error:
                fdx_lines.append(f"- Warning/Error: {fdx.error}")
            sections.append("\n".join(fdx_lines))

        # 5. LSP Intelligence Aggregation
        if include_lsp and (
            snapshot.lsp.enabled
            or snapshot.lsp.status != "unavailable"
            or snapshot.lsp.active_servers
        ):
            lsp = snapshot.lsp
            lsp_lines = [
                "## LSP Intelligence",
                f"- Status: {lsp.status.upper()}",
            ]
            if lsp.active_servers:
                lsp_lines.append(f"- Active Servers: {', '.join(lsp.active_servers)}")
            lsp_lines.append(f"- Diagnostics: {lsp.diagnostics_count} items")
            if lsp.diagnostics:
                for d in lsp.diagnostics[:5]:
                    path = d.get("path", "")
                    msg = d.get("message", "")
                    lsp_lines.append(f"  * {path}: {msg}")
            if lsp.symbols_count:
                lsp_lines.append(f"- Symbols Indexed: {lsp.symbols_count}")
            if lsp.error:
                lsp_lines.append(f"- Note: {lsp.error}")
            sections.append("\n".join(lsp_lines))

        # 6. Memory Context
        if include_memory and snapshot.memory.enabled:
            mem = snapshot.memory
            mem_lines = ["## Memory Context"]
            if mem.memory_version is not None:
                mem_lines.append(f"- Memory Version: {mem.memory_version}")
            if mem.canonical_memories:
                mem_lines.append("[Canonical Memory]")
                for item in mem.canonical_memories:
                    mem_lines.append(item)
            if mem.managed_context:
                mem_lines.append(mem.managed_context)
            if mem.snippets:
                mem_lines.append("- Snippets:")
                for snip in mem.snippets[:5]:
                    heading = snip.get("heading") or snip.get("title") or "Snippet"
                    snippet_text = snip.get("snippet") or snip.get("text") or ""
                    mem_lines.append(f"  * {heading}: {snippet_text[:200]}")
            if mem.diagnostics:
                for d in mem.diagnostics:
                    mem_lines.append(f"- Note: {d}")
            if mem.error:
                mem_lines.append(f"- Note: {mem.error}")
            if len(mem_lines) > 1:
                sections.append("\n".join(mem_lines))

        # 7. Environment Context
        if include_environment:
            env = snapshot.environment
            env_lines = ["## Environment Context"]
            if env.os_info:
                env_lines.append(f"- OS: {env.os_info}")
            if env.python_version:
                env_lines.append(f"- Python: {env.python_version}")
            if env.shell:
                env_lines.append(f"- Shell: {env.shell}")
            if env.hostname:
                env_lines.append(f"- Hostname: {env.hostname}")
            if env.tools:
                env_lines.append(f"- Available Tools: {', '.join(env.tools)}")
            if env.env_vars:
                safe_vars = [f"{k}={v}" for k, v in sorted(env.env_vars.items())]
                env_lines.append(f"- Environment Variables: {', '.join(safe_vars[:10])}")
            if len(env_lines) > 1:
                sections.append("\n".join(env_lines))

        # 8. Instructions
        if include_instructions:
            inst = snapshot.instructions
            inst_lines = []
            if inst.system_instructions:
                inst_lines.append(f"### System Instructions\n{inst.system_instructions}")
            if inst.task_instructions:
                inst_lines.append(f"### Task Instructions\n{inst.task_instructions}")
            if inst.user_instructions:
                inst_lines.append(f"### User Instructions\n{inst.user_instructions}")
            if inst.steering_instructions:
                inst_lines.append("### Steering Instructions")
                for s in inst.steering_instructions:
                    inst_lines.append(f"- {s}")
            if inst_lines:
                sections.append("## Instructions\n" + "\n\n".join(inst_lines))

        # 9. Bounded Workers Evidence
        if include_workers and (snapshot.workers.enabled or snapshot.workers.error):
            w = snapshot.workers
            if w.status != "unavailable" or w.workers or w.error:
                w_lines = [
                    "## Active Workers",
                    f"- Status: {w.status.upper()} "
                    f"({w.total_count} total, {w.active_count} active)",
                ]
                for worker in w.workers[:10]:
                    name = worker.get("name") or worker.get("worker_id") or "unknown"
                    status = worker.get("status") or "?"
                    changed = worker.get("changed_file_count", 0)
                    repo = worker.get("repo_path") or "."
                    w_lines.append(f"  * {name} [{status}] repo={repo} changed_files={changed}")
                if len(w.workers) > 10:
                    w_lines.append(f"  ... (+{len(w.workers) - 10} more)")
                if w.diagnostics:
                    for d in w.diagnostics:
                        w_lines.append(f"- Note: {d}")
                if w.error:
                    w_lines.append(f"- Note: {w.error}")
                sections.append("\n".join(w_lines))
            elif w.error:
                sections.append(f"## Active Workers\n- Note: {w.error}")

        # 10. Project Evidence
        if include_project and snapshot.project.enabled:
            proj = snapshot.project
            if proj.status != "unavailable" or proj.manifest_files:
                p_lines = ["## Project Evidence"]
                if proj.detected_language:
                    p_lines.append(f"- Detected Language: {proj.detected_language}")
                if proj.manifest_files:
                    p_lines.append(f"- Manifests: {', '.join(proj.manifest_files[:10])}")
                if proj.test_files:
                    sample = proj.test_files[:5]
                    suffix = (
                        f" (+{len(proj.test_files) - 5} more)" if len(proj.test_files) > 5 else ""
                    )
                    p_lines.append(f"- Test Files: {', '.join(sample)}{suffix}")
                if proj.script_files:
                    sample = proj.script_files[:5]
                    suffix = (
                        f" (+{len(proj.script_files) - 5} more)"
                        if len(proj.script_files) > 5
                        else ""
                    )
                    p_lines.append(f"- Scripts: {', '.join(sample)}{suffix}")
                if proj.file_count:
                    p_lines.append(f"- Files Scanned: {proj.file_count}")
                for diag in proj.diagnostics:
                    p_lines.append(f"- Note: {diag}")
                if proj.error:
                    p_lines.append(f"- Error: {proj.error}")
                if len(p_lines) > 1:
                    sections.append("\n".join(p_lines))

        # 11. Explicit Degraded Subsystem Diagnostics
        if include_diagnostics and snapshot.diagnostics:
            diag_lines = ["## Degraded Subsystem Diagnostics"]
            for diag in snapshot.diagnostics:
                diag_lines.append(f"- ⚠️ {diag}")
            sections.append("\n".join(diag_lines))

        rendered = "\n\n".join(sections)

        # Character budget bounding
        if max_chars is not None and len(rendered) > max_chars:
            truncation_notice = (
                f"\n\n[...context truncated to fit character limit ({max_chars} chars)...]"
            )
            allowed_chars = max(100, max_chars - len(truncation_notice))
            rendered = rendered[:allowed_chars] + truncation_notice

        return rendered

    def compile_bundle(
        self,
        snapshot: WorkspaceContextSnapshot,
        *,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        rendered = self.compile(snapshot, max_chars=max_chars)
        return {
            "snapshot_id": snapshot.snapshot_id,
            "version": snapshot.version,
            "content_digest": snapshot.content_digest,
            "workspace_id": snapshot.workspace_id,
            "active_workspace_id": snapshot.active_workspace_id,
            "workspace_root": snapshot.workspace_root,
            "revision": snapshot.repo.revision,
            "is_dirty": snapshot.repo.is_dirty,
            "is_diverged": snapshot.divergence.is_diverged,
            "is_stale": snapshot.divergence.is_stale,
            "char_count": len(rendered),
            "rendered": rendered,
            "diagnostics": list(snapshot.diagnostics),
            "created_at_ms": snapshot.created_at_ms,
        }


class WorkspaceContextSnapshotService:
    """Service to capture live workspace evidence, evaluate divergence, and compile context."""

    def __init__(
        self,
        *,
        compiler: WorkspaceContextCompilerProtocol | None = None,
        git_module: Any | None = None,
        fdx_service: Any | None = None,
        lsp_service: Any | None = None,
        memory_service: Any | None = None,
        workbench_store: Any | None = None,
    ) -> None:
        self._compiler = compiler or WorkspaceContextCompiler()
        self._git_module = git_module
        self._fdx_service = fdx_service
        self._lsp_service = lsp_service
        self._memory_service = memory_service
        self._workbench_store = workbench_store

    def _get_git_module(self) -> Any:
        if self._git_module is not None:
            return self._git_module
        try:
            import cptr.utils.git as git

            return git
        except Exception:
            return None

    def _get_fdx_service(self) -> Any:
        if self._fdx_service is not None:
            return self._fdx_service
        try:
            from cptr.services.fdx_intelligence import service as fdx

            return fdx
        except Exception:
            return None

    def _get_lsp_service(self) -> Any:
        if self._lsp_service is not None:
            return self._lsp_service
        try:
            from cptr.services.automatic_lsp_intelligence import service as lsp

            return lsp
        except Exception:
            return None

    def _get_memory_service(self) -> Any:
        if self._memory_service is not None:
            return self._memory_service
        try:
            # Can be resolved on demand if needed
            return None
        except Exception:
            return None

    def _get_workbench_store(self) -> Any:
        if self._workbench_store is not None:
            return self._workbench_store
        try:
            from cptr.services.workbench_sessions import workbench_session_store

            return workbench_session_store
        except Exception:
            return None

    async def capture_snapshot(
        self,
        *,
        workspace_root: str | Path,
        user_id: str | None = None,
        workspace_id: str | None = None,
        active_workspace_id: str | None = None,
        workbench_session: Any | None = None,
        workbench_session_id: str | None = None,
        checkpoint: Any | None = None,
        memory_input: Any | None = None,
        environment_input: dict[str, Any] | None = None,
        instruction_input: str | dict[str, Any] | InstructionContext | None = None,
        include_repo: bool = True,
        include_fdx: bool = True,
        include_lsp: bool = True,
        include_memory: bool = True,
        include_environment: bool = True,
        include_workers: bool = True,
        include_project: bool = True,
        include_diagnostics: bool = True,
        fdx_options: dict[str, Any] | None = None,
        lsp_options: dict[str, Any] | None = None,
    ) -> WorkspaceContextSnapshot:
        """Capture a WorkspaceContextSnapshot composing all available inputs and evidence."""
        resolved_root = str(Path(workspace_root).resolve())

        # 1. Resolve workspace_id and active_workspace_id preserving Workbench semantics
        final_workspace_id = workspace_id
        final_active_workspace_id = active_workspace_id

        # If a WorkbenchSession object or dict was passed, extract its IDs
        if workbench_session is not None:
            if isinstance(workbench_session, dict):
                ws_id = workbench_session.get("workspace_id")
                active_ws_id = workbench_session.get("active_workspace_id")
                u_id = workbench_session.get("user_id") or workbench_session.get("owner_id")
            else:
                ws_id = getattr(workbench_session, "workspace_id", None)
                active_ws_id = getattr(workbench_session, "active_workspace_id", None)
                u_id = getattr(workbench_session, "user_id", None)
            if final_workspace_id is None and ws_id:
                final_workspace_id = ws_id
            if final_active_workspace_id is None:
                final_active_workspace_id = active_ws_id
            if user_id is None and u_id:
                user_id = u_id
        elif workbench_session_id and user_id:
            store = self._get_workbench_store()
            if store is not None:
                try:
                    loaded_session = await store.get(
                        owner_id=user_id, session_id=workbench_session_id
                    )
                    if loaded_session:
                        if final_workspace_id is None:
                            final_workspace_id = loaded_session.get("workspace_id")
                        if final_active_workspace_id is None:
                            final_active_workspace_id = loaded_session.get("active_workspace_id")
                except Exception:
                    pass

        # Semantics: if active_workspace_id was not explicitly specified or resolved, default to workspace_id
        if final_active_workspace_id is None and final_workspace_id is not None:
            final_active_workspace_id = final_workspace_id

        # 2. Capture live repository evidence
        repo_evidence = RepoEvidence()
        if include_repo:
            repo_evidence = await self._capture_repo_evidence(resolved_root)

        # 3. Evaluate Stale Checkpoint Divergence
        divergence = await self._evaluate_divergence(
            resolved_root=resolved_root,
            repo_evidence=repo_evidence,
            checkpoint=checkpoint,
            memory_input=memory_input,
        )

        # 4. Aggregated FDX Intelligence
        fdx_evidence = FdxEvidence()
        if include_fdx:
            fdx_evidence = await self._capture_fdx_evidence(
                resolved_root=resolved_root,
                user_id=user_id or "anonymous",
                workspace_id=final_workspace_id or "default",
                options=fdx_options or {},
            )

        # 5. Aggregated LSP Intelligence
        lsp_evidence = LspEvidence()
        if include_lsp:
            lsp_evidence = await self._capture_lsp_evidence(
                resolved_root=resolved_root,
                options=lsp_options or {},
            )

        # 6. Optional Memory Inputs
        memory_evidence = MemoryEvidence()
        if include_memory:
            memory_evidence = await self._capture_memory_evidence(
                user_id=user_id,
                workspace_root=resolved_root,
                memory_input=memory_input,
            )

        # 7. Optional Environment Inputs
        env_evidence = EnvironmentEvidence()
        if include_environment:
            env_evidence = self._capture_environment_evidence(environment_input)

        # 8. Optional Instruction Inputs
        instruction_ctx = self._capture_instruction_context(instruction_input)

        # 9. Bounded Workers Evidence
        workers_evidence = BoundedWorkersEvidence()
        if include_workers:
            workers_evidence = await self._capture_workers_evidence(
                workspace_root=resolved_root,
                user_id=user_id,
                workspace_id=final_workspace_id,
            )

        # 10. Project/Test/Script Evidence
        project_evidence = ProjectEvidence()
        if include_project:
            project_evidence = await self._capture_project_evidence(resolved_root)

        # Collect explicit degraded diagnostics across all subsystems
        aggregated_diagnostics: list[str] = []
        if repo_evidence.error:
            aggregated_diagnostics.append(f"repo: {repo_evidence.error}")
        for d in getattr(repo_evidence, "diagnostics", []):
            diag_msg = f"repo: {d}"
            if diag_msg not in aggregated_diagnostics:
                aggregated_diagnostics.append(diag_msg)

        if fdx_evidence.error:
            aggregated_diagnostics.append(f"fdx: {fdx_evidence.error}")
        for d in getattr(fdx_evidence, "diagnostics", []):
            diag_msg = f"fdx: {d}"
            if diag_msg not in aggregated_diagnostics:
                aggregated_diagnostics.append(diag_msg)

        if lsp_evidence.error:
            aggregated_diagnostics.append(f"lsp: {lsp_evidence.error}")

        if memory_evidence.error:
            aggregated_diagnostics.append(f"memory: {memory_evidence.error}")
        for d in getattr(memory_evidence, "diagnostics", []):
            diag_msg = f"memory: {d}"
            if diag_msg not in aggregated_diagnostics:
                aggregated_diagnostics.append(diag_msg)

        if workers_evidence.error:
            aggregated_diagnostics.append(f"workers: {workers_evidence.error}")
        for d in getattr(workers_evidence, "diagnostics", []):
            diag_msg = f"workers: {d}"
            if diag_msg not in aggregated_diagnostics:
                aggregated_diagnostics.append(diag_msg)

        if project_evidence.error:
            aggregated_diagnostics.append(f"project: {project_evidence.error}")
        for d in getattr(project_evidence, "diagnostics", []):
            diag_msg = f"project: {d}"
            if diag_msg not in aggregated_diagnostics:
                aggregated_diagnostics.append(diag_msg)

        snapshot = WorkspaceContextSnapshot(
            workspace_root=resolved_root,
            user_id=user_id,
            workspace_id=final_workspace_id,
            active_workspace_id=final_active_workspace_id,
            created_at_ms=_now_ms(),
            repo=repo_evidence,
            fdx=fdx_evidence,
            lsp=lsp_evidence,
            divergence=divergence,
            memory=memory_evidence,
            environment=env_evidence,
            instructions=instruction_ctx,
            workers=workers_evidence,
            project=project_evidence,
            diagnostics=aggregated_diagnostics,
        )
        return snapshot

    async def _capture_repo_evidence(self, root: str) -> RepoEvidence:
        git = self._get_git_module()
        if git is None:
            return RepoEvidence(
                is_repo=False,
                status="unavailable",
                error="git module unavailable",
                diagnostics=["git module unavailable"],
            )

        try:
            is_git = await git.is_repo(root)
            if not is_git:
                return RepoEvidence(
                    is_repo=False,
                    root=root,
                    status="unavailable",
                    diagnostics=["directory is not a git repository"],
                )

            status_dict = await git.status(root)
            rev = await git.current_revision(root)
            branch = status_dict.get("branch") or "HEAD"

            files = list(status_dict.get("files") or [])
            if files:
                staged = [f["path"] for f in files if f.get("staged")]
                unstaged = [
                    f["path"] for f in files if f.get("unstaged") and f.get("status") != "untracked"
                ]
                untracked = [f["path"] for f in files if f.get("status") == "untracked"]
            else:
                staged = list(status_dict.get("staged") or [])
                unstaged = list(status_dict.get("unstaged") or [])
                untracked = list(status_dict.get("untracked") or [])
            is_dirty = bool(status_dict.get("dirty") or staged or unstaged or untracked)

            # Get recent commits
            recent_commits: list[dict[str, Any]] = []
            repo_diagnostics: list[str] = []
            try:
                log_data = await git.log(root, limit=5)
                recent_commits = list(log_data.get("commits") or [])
            except Exception as exc:
                repo_diagnostics.append(f"recent commits retrieval degraded: {exc}")

            return RepoEvidence(
                is_repo=True,
                status="ok",
                root=root,
                branch=branch,
                revision=rev,
                is_dirty=is_dirty,
                staged_count=len(staged),
                unstaged_count=len(unstaged),
                untracked_count=len(untracked),
                modified_files=unstaged,
                staged_files=staged,
                untracked_files=untracked,
                recent_commits=recent_commits,
                diff_summary={
                    "staged_count": len(staged),
                    "unstaged_count": len(unstaged),
                    "untracked_count": len(untracked),
                },
                diagnostics=repo_diagnostics,
            )
        except Exception as exc:
            return RepoEvidence(
                is_repo=False,
                root=root,
                status="degraded",
                error=str(exc),
                diagnostics=[f"git inspection failed: {exc}"],
            )

    async def _evaluate_divergence(
        self,
        *,
        resolved_root: str,
        repo_evidence: RepoEvidence,
        checkpoint: Any | None,
        memory_input: Any | None,
    ) -> CheckpointDivergence:
        if checkpoint is None:
            return CheckpointDivergence(is_diverged=False, is_stale=False)

        # Extract checkpoint attributes (handling Checkpoint, CheckpointState, or dict)
        cp_id: str | None = None
        cp_rev: str | None = None
        cp_mem_version: int | None = None

        if isinstance(checkpoint, dict):
            cp_id = checkpoint.get("checkpoint_id") or checkpoint.get("id")
            cp_rev = (
                checkpoint.get("revision")
                or checkpoint.get("git_revision")
                or checkpoint.get("commit")
            )
            cp_mem_version = checkpoint.get("memory_version")
        elif isinstance(checkpoint, str):
            # Treat as checkpoint ID or revision
            if len(checkpoint) in (40, 64) and not checkpoint.startswith("chk_"):
                cp_rev = checkpoint
            else:
                cp_id = checkpoint
        else:
            cp_id = getattr(checkpoint, "checkpoint_id", None) or getattr(checkpoint, "id", None)
            cp_rev = getattr(checkpoint, "revision", None) or getattr(
                checkpoint, "git_revision", None
            )
            cp_mem_version = getattr(checkpoint, "memory_version", None)

        is_diverged = False
        is_stale = False
        reasons: list[str] = []
        commits_ahead = 0
        commits_behind = 0
        diverged_files: list[str] = []

        # 1. Check revision divergence
        if cp_rev and repo_evidence.is_repo:
            current_rev = repo_evidence.revision
            if current_rev and current_rev.lower() != cp_rev.lower():
                is_diverged = True
                is_stale = True
                reasons.append(
                    f"revision_mismatch: workspace at {current_rev[:8]} but checkpoint at {cp_rev[:8]}"
                )
                git = self._get_git_module()
                if git is not None:
                    try:
                        # Check commits ahead/behind
                        code_ahead, stdout_ahead, _ = await git._run(
                            "rev-list",
                            "--count",
                            f"{cp_rev}..{current_rev}",
                            cwd=resolved_root,
                            check=False,
                        )
                        if code_ahead == 0 and stdout_ahead.strip().isdigit():
                            commits_ahead = int(stdout_ahead.strip())

                        code_behind, stdout_behind, _ = await git._run(
                            "rev-list",
                            "--count",
                            f"{current_rev}..{cp_rev}",
                            cwd=resolved_root,
                            check=False,
                        )
                        if code_behind == 0 and stdout_behind.strip().isdigit():
                            commits_behind = int(stdout_behind.strip())

                        # Check changed files between revisions
                        code_diff, stdout_diff, _ = await git._run(
                            "diff",
                            "--name-only",
                            cp_rev,
                            current_rev,
                            cwd=resolved_root,
                            check=False,
                        )
                        if code_diff == 0:
                            diff_files = [f.strip() for f in stdout_diff.splitlines() if f.strip()]
                            diverged_files.extend(diff_files)
                    except Exception:
                        pass

        # 2. Check uncommitted changes divergence
        if repo_evidence.is_dirty:
            is_diverged = True
            dirty_count = repo_evidence.staged_count + repo_evidence.unstaged_count
            reasons.append(f"dirty_working_tree: {dirty_count} uncommitted modification(s) present")
            for f in repo_evidence.modified_files + repo_evidence.staged_files:
                if f not in diverged_files:
                    diverged_files.append(f)

        # 3. Check memory drift
        current_mem_ver = None
        if isinstance(memory_input, dict):
            current_mem_ver = memory_input.get("memory_version")
        elif hasattr(memory_input, "memory_version"):
            current_mem_ver = getattr(memory_input, "memory_version")

        memory_drift = 0
        if cp_mem_version is not None and current_mem_ver is not None:
            memory_drift = abs(int(current_mem_ver) - int(cp_mem_version))
            if memory_drift > 0:
                is_diverged = True
                reasons.append(
                    f"memory_drift: current memory version {current_mem_ver} diverges from checkpoint {cp_mem_version}"
                )

        summary = (
            f"Diverged ({', '.join(reasons)})"
            if is_diverged
            else "Workspace matches checkpoint baseline."
        )

        return CheckpointDivergence(
            checkpoint_id=cp_id,
            checkpoint_revision=cp_rev,
            checkpoint_memory_version=cp_mem_version,
            is_diverged=is_diverged,
            is_stale=is_stale or is_diverged,
            commits_ahead=commits_ahead,
            commits_behind=commits_behind,
            diverged_files=diverged_files,
            memory_drift=memory_drift,
            divergence_reasons=reasons,
            summary=summary,
        )

    async def _capture_fdx_evidence(
        self,
        *,
        resolved_root: str,
        user_id: str,
        workspace_id: str,
        options: dict[str, Any],
    ) -> FdxEvidence:
        fdx = self._get_fdx_service()
        if fdx is None:
            return FdxEvidence(
                enabled=False,
                status="unavailable",
                diagnostics=["FDX service unavailable"],
            )

        try:
            # Run status check with short timeout to prevent blocking
            status_task = fdx.execute(
                user_id=user_id,
                workspace_id=workspace_id,
                root=Path(resolved_root),
                identity=None,  # default identity
                action="status",
                options=options,
            )
            res = await asyncio.wait_for(status_task, timeout=1.5)
            status_val = res.get("status") or ("ready" if res.get("ready") else "degraded")
            version_text = None
            if "version" in res:
                v = res["version"]
                version_text = v.get("text") if isinstance(v, dict) else str(v)

            fdx_diagnostics: list[str] = []
            if status_val == "degraded":
                reason = (
                    res.get("reason") or res.get("error_code") or "FDX returned degraded status"
                )
                fdx_diagnostics.append(str(reason))

            return FdxEvidence(
                enabled=True,
                status=str(status_val),
                daemon_running=bool(res.get("daemon_running", False)),
                version=version_text,
                capabilities=list(res.get("capabilities") or []),
                semantic_status=dict(res.get("semantic_status") or {}),
                index_status=dict(res.get("index_status") or {}),
                impact_summary=dict(res.get("impact_summary") or {}),
                metadata=res,
                diagnostics=fdx_diagnostics,
            )
        except Exception as exc:
            return FdxEvidence(
                enabled=False,
                status="unavailable",
                error=f"FDX aggregation skipped: {exc}",
                diagnostics=[f"FDX aggregation degraded: {exc}"],
            )

    async def _capture_lsp_evidence(
        self,
        *,
        resolved_root: str,
        options: dict[str, Any],
    ) -> LspEvidence:
        lsp = self._get_lsp_service()
        if lsp is None:
            return LspEvidence(enabled=False, status="unavailable")

        try:
            # Check warm sessions or active language servers
            active_servers: list[str] = []
            if hasattr(lsp, "_sessions"):
                for session in lsp._sessions.values():
                    if (
                        str(session.root) == resolved_root
                        and session.server_id not in active_servers
                    ):
                        active_servers.append(session.server_id)

            status = (
                "ready"
                if active_servers
                else ("idle" if getattr(lsp, "_enabled", True) else "disabled")
            )
            return LspEvidence(
                enabled=getattr(lsp, "_enabled", True),
                status=status,
                active_servers=active_servers,
                diagnostics_count=0,
                symbols_count=0,
            )
        except Exception as exc:
            return LspEvidence(
                enabled=False,
                status="unavailable",
                error=f"LSP aggregation skipped: {exc}",
            )

    async def _capture_memory_evidence(
        self,
        *,
        user_id: str | None,
        workspace_root: str,
        memory_input: Any | None,
    ) -> MemoryEvidence:
        if memory_input is None:
            return MemoryEvidence(enabled=True, status="ok")

        # 1. If a list of strings / canonical memories was provided
        if isinstance(memory_input, list):
            canonical = [str(item) for item in memory_input]
            return MemoryEvidence(
                enabled=True,
                status="ok",
                canonical_memories=canonical,
            )

        # 2. If a dictionary was provided
        if isinstance(memory_input, dict):
            return MemoryEvidence.from_dict(memory_input)

        # 3. If a MemoryContextBundle or domain object was provided
        canonical_lines: list[str] = []
        rendered = getattr(memory_input, "rendered", None)
        mem_version = getattr(memory_input, "memory_version", None)
        items = getattr(memory_input, "items", [])

        if rendered:
            for line in rendered.splitlines():
                if line.startswith("- ["):
                    canonical_lines.append(line)

        return MemoryEvidence(
            enabled=True,
            status="ok",
            memory_version=mem_version,
            canonical_memories=canonical_lines,
            managed_context=rendered,
            snippets=list(items) if isinstance(items, list) else [],
        )

    async def _capture_workers_evidence(
        self,
        *,
        workspace_root: str,
        user_id: str | None,
        workspace_id: str | None,
    ) -> BoundedWorkersEvidence:
        """Capture bounded evidence of active DirectCodingWorkers for the workspace.

        Paths are redacted in the returned evidence; only safe identity fields
        are preserved. Partial failures degrade to explicit diagnostics.
        """
        if not workspace_id or not user_id:
            return BoundedWorkersEvidence(
                enabled=False,
                status="unavailable",
                diagnostics=["workspace_id or user_id not resolved; workers evidence skipped"],
                error="workspace_id or user_id not resolved",
            )

        try:
            from cptr.services.direct_coding_workers import service as _dcw_service

            summaries: list[dict[str, Any]] = await _dcw_service.list(
                user_id=user_id,
                workspace_id=workspace_id,
            )

            # Keep only safe, non-path fields; strip worktree_path and branch details
            safe_workers: list[dict[str, Any]] = []
            active_statuses = {"READY", "WORKING", "RUNNING"}
            for w in summaries[:20]:  # bounded: at most 20 entries
                safe_workers.append(
                    {
                        "worker_id": str(w.get("worker_id") or ""),
                        "name": str(w.get("name") or "")[:80],
                        "responsibility": str(w.get("responsibility") or "")[:200],
                        "repo_path": str(w.get("repo_path") or "."),
                        "status": str(w.get("status") or ""),
                        "changed_file_count": int(w.get("changed_file_count") or 0),
                        "active_command_count": len(list(w.get("active_command_ids") or [])),
                        "created_at": w.get("created_at"),
                    }
                )

            active_count = sum(1 for w in safe_workers if w.get("status") in active_statuses)
            return BoundedWorkersEvidence(
                enabled=True,
                status="ok",
                total_count=len(safe_workers),
                active_count=active_count,
                workers=safe_workers,
            )
        except Exception as exc:
            return BoundedWorkersEvidence(
                enabled=False,
                status="degraded",
                diagnostics=[f"workers evidence degraded: {exc}"],
                error=f"workers evidence degraded: {exc}",
            )

    async def _capture_project_evidence(
        self,
        workspace_root: str,
    ) -> ProjectEvidence:
        """Discover project manifests, test files, and script files.

        Discovery is bounded (max 500 files scanned), workspace-relative,
        and never raises; partial failures degrade to explicit diagnostics.
        """
        import fnmatch

        root = Path(workspace_root)
        if not root.is_dir():
            return ProjectEvidence(
                enabled=False,
                status="unavailable",
                error="workspace root is not a directory",
            )

        # Known manifest patterns and their associated language signals
        _MANIFEST_PATTERNS: list[tuple[str, str]] = [
            ("pyproject.toml", "python"),
            ("setup.py", "python"),
            ("setup.cfg", "python"),
            ("requirements.txt", "python"),
            ("package.json", "javascript"),
            ("Cargo.toml", "rust"),
            ("go.mod", "go"),
            ("pom.xml", "java"),
            ("build.gradle", "java"),
            ("build.gradle.kts", "java"),
            ("CMakeLists.txt", "cpp"),
            ("Makefile", "make"),
            ("composer.json", "php"),
            ("Gemfile", "ruby"),
            ("mix.exs", "elixir"),
            ("pubspec.yaml", "dart"),
            ("tsconfig.json", "typescript"),
            ("Dockerfile", "docker"),
        ]
        _TEST_GLOBS = [
            "test_*.py",
            "*_test.py",
            "*.test.ts",
            "*.spec.ts",
            "*.test.js",
            "*.spec.js",
            "*.test.tsx",
            "*.spec.tsx",
        ]
        _SCRIPT_GLOBS = ["*.sh", "*.bash", "*.zsh", "*.fish", "*.ps1", "scripts/*.py", "bin/*.py"]
        _SKIP_DIRS = {
            ".git",
            "__pycache__",
            "node_modules",
            ".venv",
            "venv",
            ".tox",
            "dist",
            "build",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".cargo",
        }

        manifest_files: list[str] = []
        test_files: list[str] = []
        script_files: list[str] = []
        source_roots: list[str] = []
        diagnostics: list[str] = []
        file_count = 0
        detected_language: str | None = None
        language_votes: dict[str, int] = {}

        _MAX_FILES = 500

        try:
            for dirpath, dirnames, filenames in os.walk(root):
                # Prune skip directories in-place
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]

                for filename in filenames:
                    if file_count >= _MAX_FILES:
                        diagnostics.append(
                            f"file scan bounded at {_MAX_FILES}; some files not examined"
                        )
                        break

                    try:
                        abs_path = os.path.join(dirpath, filename)
                        rel = os.path.relpath(abs_path, root)
                    except Exception:
                        continue

                    file_count += 1

                    # Check manifests
                    for pattern, lang in _MANIFEST_PATTERNS:
                        if filename == pattern:
                            manifest_files.append(rel)
                            language_votes[lang] = language_votes.get(lang, 0) + 1
                            break

                    # Check test files
                    for glob in _TEST_GLOBS:
                        if fnmatch.fnmatch(filename, glob):
                            if len(test_files) < 50:
                                test_files.append(rel)
                            break

                    # Check scripts
                    for glob in _SCRIPT_GLOBS:
                        if fnmatch.fnmatch(filename, glob) or fnmatch.fnmatch(rel, glob):
                            if len(script_files) < 30:
                                script_files.append(rel)
                            break

                if file_count >= _MAX_FILES:
                    break

            # Detect source roots (directories containing manifest files)
            for mf in manifest_files:
                parent = str(Path(mf).parent)
                sr = "." if parent == "." else parent
                if sr not in source_roots:
                    source_roots.append(sr)

            # Pick dominant language
            if language_votes:
                detected_language = max(language_votes, key=lambda k: language_votes[k])

            return ProjectEvidence(
                enabled=True,
                status="ok",
                detected_language=detected_language,
                manifest_files=sorted(manifest_files),
                test_files=sorted(test_files),
                script_files=sorted(script_files),
                source_roots=sorted(source_roots),
                file_count=file_count,
                diagnostics=diagnostics,
            )
        except Exception as exc:
            return ProjectEvidence(
                enabled=True,
                status="degraded",
                file_count=file_count,
                diagnostics=diagnostics,
                error=f"project discovery failed: {exc}",
            )

    def _capture_environment_evidence(
        self,
        environment_input: dict[str, Any] | None,
    ) -> EnvironmentEvidence:
        base_env: dict[str, Any] = environment_input or {}
        hostname = base_env.get("hostname") or platform.node()
        os_info = base_env.get("os_info") or f"{platform.system()} {platform.release()}"
        arch = base_env.get("architecture") or platform.machine()
        shell = base_env.get("shell") or os.environ.get("SHELL", "")
        py_ver = base_env.get("python_version") or sys.version.split()[0]

        # Safe redacted environment variables
        safe_keys = {"PATH", "SHELL", "LANG", "LC_ALL", "USER", "TERM", "VIRTUAL_ENV", "EDITOR"}
        collected_vars: dict[str, str] = {}
        for k, v in os.environ.items():
            if k in safe_keys:
                collected_vars[k] = v
        if "env_vars" in base_env and isinstance(base_env["env_vars"], dict):
            collected_vars.update(base_env["env_vars"])

        redacted_vars = redact_sensitive(collected_vars)

        tools = list(base_env.get("tools") or ["python", "git"])
        return EnvironmentEvidence(
            hostname=hostname,
            os_info=os_info,
            architecture=arch,
            shell=shell,
            python_version=py_ver,
            env_vars=redacted_vars,
            tools=tools,
            extra=dict(base_env.get("extra") or {}),
        )

    def _capture_instruction_context(
        self,
        instruction_input: str | dict[str, Any] | InstructionContext | None,
    ) -> InstructionContext:
        if instruction_input is None:
            return InstructionContext()
        if isinstance(instruction_input, InstructionContext):
            return instruction_input
        if isinstance(instruction_input, str):
            return InstructionContext(task_instructions=instruction_input)
        if isinstance(instruction_input, dict):
            return InstructionContext.from_dict(instruction_input)
        return InstructionContext(task_instructions=str(instruction_input))

    def compile(
        self,
        snapshot: WorkspaceContextSnapshot,
        *,
        max_chars: int | None = None,
        include_repo: bool = True,
        include_fdx: bool = True,
        include_lsp: bool = True,
        include_divergence: bool = True,
        include_memory: bool = True,
        include_environment: bool = True,
        include_instructions: bool = True,
        include_workers: bool = True,
        include_project: bool = True,
        include_diagnostics: bool = True,
    ) -> str:
        """Compile a snapshot using the configured compiler."""
        return self._compiler.compile(
            snapshot,
            max_chars=max_chars,
            include_repo=include_repo,
            include_fdx=include_fdx,
            include_lsp=include_lsp,
            include_divergence=include_divergence,
            include_memory=include_memory,
            include_environment=include_environment,
            include_instructions=include_instructions,
            include_workers=include_workers,
            include_project=include_project,
            include_diagnostics=include_diagnostics,
        )

    def compile_bundle(
        self,
        snapshot: WorkspaceContextSnapshot,
        *,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Compile a snapshot bundle using the configured compiler."""
        return self._compiler.compile_bundle(snapshot, max_chars=max_chars)

    async def check_divergence(
        self,
        *,
        workspace_root: str | Path,
        checkpoint: Any,
    ) -> CheckpointDivergence:
        """Standalone helper to check divergence against a checkpoint."""
        resolved = str(Path(workspace_root).resolve())
        repo_evidence = await self._capture_repo_evidence(resolved)
        return await self._evaluate_divergence(
            resolved_root=resolved,
            repo_evidence=repo_evidence,
            checkpoint=checkpoint,
            memory_input=None,
        )


# Aliases and singleton
WorkspaceContextService = WorkspaceContextSnapshotService
workspace_context_service = WorkspaceContextSnapshotService()

__all__ = [
    "BoundedWorkersEvidence",
    "CheckpointDivergence",
    "EnvironmentEvidence",
    "FdxEvidence",
    "InstructionContext",
    "LspEvidence",
    "MemoryEvidence",
    "ProjectEvidence",
    "RepoEvidence",
    "WorkspaceContextCompiler",
    "WorkspaceContextCompilerProtocol",
    "WorkspaceContextService",
    "WorkspaceContextSnapshot",
    "WorkspaceContextSnapshotService",
    "workspace_context_service",
]
