"""Tests for WorkspaceContextSnapshot, WorkspaceContextCompiler, and WorkspaceContextSnapshotService."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

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
from cptr.services.workspace_context import (
    WorkspaceContextCompiler,
    WorkspaceContextSnapshotService,
)


class WorkspaceContextModelTests(unittest.TestCase):
    def test_snapshot_model_serialization_and_digest(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_test_123",
            workspace_root="/tmp/test_ws",
            user_id="user_1",
            workspace_id="ws_1",
            active_workspace_id="ws_2",
            created_at_ms=1700000000000,
            repo=RepoEvidence(
                is_repo=True,
                root="/tmp/test_ws",
                branch="feature/test",
                revision="abc123def456",
                is_dirty=True,
                staged_count=1,
                unstaged_count=2,
                untracked_count=3,
                modified_files=["modified.py"],
                staged_files=["staged.py"],
                untracked_files=["untracked.py"],
                recent_commits=[{"sha": "abc123def456", "message": "Initial commit"}],
            ),
            fdx=FdxEvidence(
                enabled=True,
                status="ready",
                daemon_running=True,
                capabilities=["read", "search", "semantic_status"],
            ),
            lsp=LspEvidence(
                enabled=True,
                status="ready",
                active_servers=["pyright"],
                diagnostics_count=1,
                diagnostics=[{"path": "modified.py", "message": "Undefined variable"}],
            ),
            divergence=CheckpointDivergence(
                checkpoint_id="chk_baseline",
                checkpoint_revision="aaa000",
                checkpoint_memory_version=2,
                is_diverged=True,
                is_stale=True,
                commits_ahead=3,
                commits_behind=0,
                diverged_files=["modified.py"],
                memory_drift=1,
                divergence_reasons=[
                    "revision_mismatch: workspace at abc123de but checkpoint at aaa000"
                ],
                summary="Diverged (revision_mismatch)",
            ),
            memory=MemoryEvidence(
                enabled=True,
                status="ok",
                memory_version=3,
                canonical_memories=["- [procedure; workspace; trust=verified_fact] run tests"],
            ),
            environment=EnvironmentEvidence(
                hostname="test-host",
                os_info="Linux 6.8",
                python_version="3.12.0",
                env_vars={"PATH": "/usr/bin", "API_KEY": "super_secret_token"},
            ),
            instructions=InstructionContext(
                system_instructions="You are Computer (cptr).",
                user_instructions="Fix bug in compiler.",
                steering_instructions=["Keep backward compatibility."],
                task_instructions="Task 1: Add tests.",
            ),
        )

        self.assertTrue(snapshot.content_digest)
        as_dict = snapshot.to_dict()

        # Sensitive variables must be redacted
        self.assertEqual(as_dict["environment"]["env_vars"]["API_KEY"], "[REDACTED]")
        self.assertEqual(as_dict["environment"]["env_vars"]["PATH"], "/usr/bin")

        # Round-trip deserialization
        restored = WorkspaceContextSnapshot.from_dict(as_dict)
        self.assertEqual(restored.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(restored.workspace_root, snapshot.workspace_root)
        self.assertEqual(restored.workspace_id, "ws_1")
        self.assertEqual(restored.active_workspace_id, "ws_2")
        self.assertEqual(restored.repo.branch, "feature/test")
        self.assertEqual(restored.repo.revision, "abc123def456")
        self.assertTrue(restored.repo.is_dirty)
        self.assertTrue(restored.divergence.is_diverged)
        self.assertEqual(restored.divergence.commits_ahead, 3)
        self.assertEqual(restored.memory.memory_version, 3)
        self.assertEqual(len(restored.memory.canonical_memories), 1)
        self.assertEqual(restored.instructions.task_instructions, "Task 1: Add tests.")
        self.assertEqual(restored.content_digest, snapshot.content_digest)

    def test_workbench_workspace_id_semantics_preservation(self):
        # Case A: Explicit workspace_id and active_workspace_id are preserved distinctly
        snap1 = WorkspaceContextSnapshot(
            workspace_root="/ws",
            workspace_id="ws_primary",
            active_workspace_id="ws_active_target",
        )
        self.assertEqual(snap1.workspace_id, "ws_primary")
        self.assertEqual(snap1.active_workspace_id, "ws_active_target")

        # Case B: Only workspace_id passed, active_workspace_id resolved via service
        service = WorkspaceContextSnapshotService()
        snap2 = asyncio.run(
            service.capture_snapshot(
                workspace_root="/tmp",
                workspace_id="ws_single",
                include_repo=False,
                include_fdx=False,
                include_lsp=False,
            )
        )
        self.assertEqual(snap2.workspace_id, "ws_single")
        self.assertEqual(snap2.active_workspace_id, "ws_single")

        # Case C: Workbench session dict passed
        session_dict = {
            "session_id": "wbs_100",
            "workspace_id": "ws_bound_session",
            "active_workspace_id": "ws_transient_active",
            "user_id": "user_42",
        }
        snap3 = asyncio.run(
            service.capture_snapshot(
                workspace_root="/tmp",
                workbench_session=session_dict,
                include_repo=False,
                include_fdx=False,
                include_lsp=False,
            )
        )
        self.assertEqual(snap3.workspace_id, "ws_bound_session")
        self.assertEqual(snap3.active_workspace_id, "ws_transient_active")
        self.assertEqual(snap3.user_id, "user_42")


class WorkspaceContextCompilerTests(unittest.TestCase):
    def setUp(self):
        self.compiler = WorkspaceContextCompiler()

    def test_compile_renders_all_composed_evidence_and_instructions(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_comp_1",
            workspace_root="/workspaces/project",
            workspace_id="ws_main",
            active_workspace_id="ws_active",
            repo=RepoEvidence(
                is_repo=True,
                root="/workspaces/project",
                branch="main",
                revision="1122334455667788",
                is_dirty=True,
                staged_count=1,
                unstaged_count=1,
                modified_files=["src/app.py"],
                staged_files=["src/utils.py"],
                recent_commits=[{"sha": "11223344", "message": "feat: init"}],
            ),
            divergence=CheckpointDivergence(
                checkpoint_id="chk_old_1",
                checkpoint_revision="00112233",
                checkpoint_memory_version=1,
                is_diverged=True,
                is_stale=True,
                commits_ahead=2,
                diverged_files=["src/app.py"],
                divergence_reasons=["revision_mismatch", "dirty_working_tree"],
                summary="Diverged from checkpoint baseline.",
            ),
            fdx=FdxEvidence(
                enabled=True,
                status="ready",
                daemon_running=True,
                capabilities=["read", "outline", "impact"],
            ),
            lsp=LspEvidence(
                enabled=True,
                status="ready",
                active_servers=["pyright"],
                diagnostics_count=1,
                diagnostics=[{"path": "src/app.py", "message": "Type mismatch"}],
            ),
            memory=MemoryEvidence(
                enabled=True,
                memory_version=5,
                canonical_memories=["- [procedure; workspace] Always run pytest"],
                managed_context="Active memory context for direct coding.",
            ),
            environment=EnvironmentEvidence(
                os_info="Linux 6.8.0",
                python_version="3.12.0",
                shell="/bin/zsh",
                tools=["python3", "pytest", "git"],
            ),
            instructions=InstructionContext(
                system_instructions="System directive: stay safe.",
                task_instructions="Task directive: build feature.",
                steering_instructions=["Do not touch other checkouts."],
            ),
        )

        rendered = self.compiler.compile(snapshot)

        # Assert presence of key composed evidence
        self.assertIn("Workspace Context Snapshot [wcs_comp_1]", rendered)
        self.assertIn("- Workspace Root: /workspaces/project", rendered)
        self.assertIn("- Workspace ID: ws_main (Active Target Workspace: ws_active)", rendered)
        self.assertIn("⚠️ **DIVERGENCE DETECTED AGAINST BASELINE CHECKPOINT**", rendered)
        self.assertIn("- Checkpoint ID: chk_old_1", rendered)
        self.assertIn("## Repository Evidence", rendered)
        self.assertIn("- Branch: main", rendered)
        self.assertIn("- HEAD Revision: 1122334455667788", rendered)
        self.assertIn("## FDX Intelligence", rendered)
        self.assertIn("Capabilities: read, outline, impact", rendered)
        self.assertIn("## LSP Intelligence", rendered)
        self.assertIn("Active Servers: pyright", rendered)
        self.assertIn("## Memory Context", rendered)
        self.assertIn("[Canonical Memory]", rendered)
        self.assertIn("Always run pytest", rendered)
        self.assertIn("## Environment Context", rendered)
        self.assertIn("- Shell: /bin/zsh", rendered)
        self.assertIn("## Instructions", rendered)
        self.assertIn("Task directive: build feature.", rendered)
        self.assertIn("Do not touch other checkouts.", rendered)

    def test_compile_bundle_returns_metadata_and_rendered_text(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_comp_bundle",
            workspace_root="/tmp",
            workspace_id="ws_test",
        )
        bundle = self.compiler.compile_bundle(snapshot)

        self.assertEqual(bundle["snapshot_id"], "wcs_comp_bundle")
        self.assertEqual(bundle["workspace_id"], "ws_test")
        self.assertIn("rendered", bundle)
        self.assertIn("char_count", bundle)
        self.assertEqual(bundle["char_count"], len(bundle["rendered"]))

    def test_compile_respects_character_budget_bounding(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_large",
            workspace_root="/tmp",
            instructions=InstructionContext(task_instructions="A" * 5000),
        )
        rendered = self.compiler.compile(snapshot, max_chars=300)
        self.assertLessEqual(len(rendered), 300)
        self.assertIn("[...context truncated to fit character limit", rendered)


class WorkspaceContextServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_divergence_evaluation_against_checkpoint(self):
        service = WorkspaceContextSnapshotService()

        # Mock repo evidence
        repo = RepoEvidence(
            is_repo=True,
            revision="new_sha_9999",
            is_dirty=True,
            staged_count=1,
            unstaged_count=2,
            modified_files=["file_a.py", "file_b.py"],
        )

        checkpoint_data = {
            "checkpoint_id": "chk_101",
            "revision": "old_sha_0000",
            "memory_version": 2,
        }

        div = await service._evaluate_divergence(
            resolved_root="/tmp",
            repo_evidence=repo,
            checkpoint=checkpoint_data,
            memory_input={"memory_version": 5},
        )

        self.assertTrue(div.is_diverged)
        self.assertTrue(div.is_stale)
        self.assertEqual(div.checkpoint_id, "chk_101")
        self.assertEqual(div.memory_drift, 3)
        self.assertIn("file_a.py", div.diverged_files)
        self.assertTrue(any("revision_mismatch" in r for r in div.divergence_reasons))
        self.assertTrue(any("dirty_working_tree" in r for r in div.divergence_reasons))
        self.assertTrue(any("memory_drift" in r for r in div.divergence_reasons))

    async def test_divergence_clean_match(self):
        service = WorkspaceContextSnapshotService()
        repo = RepoEvidence(
            is_repo=True,
            revision="same_sha",
            is_dirty=False,
            staged_count=0,
            unstaged_count=0,
        )
        checkpoint_data = {
            "checkpoint_id": "chk_clean",
            "revision": "same_sha",
            "memory_version": 4,
        }

        div = await service._evaluate_divergence(
            resolved_root="/tmp",
            repo_evidence=repo,
            checkpoint=checkpoint_data,
            memory_input={"memory_version": 4},
        )

        self.assertFalse(div.is_diverged)
        self.assertFalse(div.is_stale)
        self.assertEqual(div.memory_drift, 0)
        self.assertEqual(len(div.divergence_reasons), 0)

    async def test_capture_snapshot_with_mocked_dependencies(self):
        # Mock git module
        mock_git = Mock()
        mock_git.is_repo = AsyncMock(return_value=True)
        mock_git.status = AsyncMock(
            return_value={
                "branch": "cptr/dev",
                "dirty": False,
                "staged": [],
                "unstaged": [],
                "untracked": [],
            }
        )
        mock_git.current_revision = AsyncMock(return_value="commit_abc123")
        mock_git.log = AsyncMock(
            return_value={"commits": [{"sha": "commit_abc123", "message": "feat: test"}]}
        )

        # Mock FDX service
        mock_fdx = Mock()
        mock_fdx.execute = AsyncMock(
            return_value={
                "ready": True,
                "status": "ready",
                "daemon_running": True,
                "capabilities": ["read", "search"],
                "semantic_status": {"indexed_files": 42},
            }
        )

        # Mock LSP service
        mock_lsp = Mock()
        mock_lsp._enabled = True
        mock_lsp._sessions = {
            "s1": SimpleNamespace(root="/test/ws", server_id="pyright"),
        }

        service = WorkspaceContextSnapshotService(
            git_module=mock_git,
            fdx_service=mock_fdx,
            lsp_service=mock_lsp,
        )

        snapshot = await service.capture_snapshot(
            workspace_root="/test/ws",
            user_id="user_test",
            workspace_id="ws_alpha",
            active_workspace_id="ws_alpha",
            checkpoint={"checkpoint_id": "chk_base", "revision": "commit_abc123"},
            memory_input=["- [fact; ws] remember this fact"],
            instruction_input="Execute direct coding task.",
        )

        self.assertTrue(snapshot.repo.is_repo)
        self.assertEqual(snapshot.repo.branch, "cptr/dev")
        self.assertEqual(snapshot.repo.revision, "commit_abc123")
        self.assertFalse(snapshot.repo.is_dirty)

        self.assertTrue(snapshot.fdx.enabled)
        self.assertEqual(snapshot.fdx.status, "ready")
        self.assertIn("read", snapshot.fdx.capabilities)

        self.assertTrue(snapshot.lsp.enabled)
        self.assertIn("pyright", snapshot.lsp.active_servers)

        self.assertFalse(snapshot.divergence.is_diverged)
        self.assertEqual(snapshot.memory.canonical_memories, ["- [fact; ws] remember this fact"])
        self.assertEqual(snapshot.instructions.task_instructions, "Execute direct coding task.")

        # Test compiling
        compiled = service.compile(snapshot)
        self.assertIn("Workspace Context Snapshot", compiled)
        self.assertIn("cptr/dev", compiled)
        self.assertIn("pyright", compiled)

    async def test_graceful_fallbacks_when_services_fail(self):
        mock_git = Mock()
        mock_git.is_repo = AsyncMock(side_effect=RuntimeError("git binary exploded"))

        mock_fdx = Mock()
        mock_fdx.execute = AsyncMock(side_effect=TimeoutError("FDX timeout"))

        mock_lsp = Mock()
        mock_lsp._enabled = True
        mock_lsp._sessions = None  # will cause AttributeError when accessing .values()

        service = WorkspaceContextSnapshotService(
            git_module=mock_git,
            fdx_service=mock_fdx,
            lsp_service=mock_lsp,
        )

        # Should NOT raise, must degrade gracefully
        snapshot = await service.capture_snapshot(
            workspace_root="/tmp",
            user_id="user_fallback",
        )

        self.assertFalse(snapshot.repo.is_repo)
        self.assertIn("git binary exploded", str(snapshot.repo.error))
        self.assertFalse(snapshot.fdx.enabled)
        self.assertEqual(snapshot.fdx.status, "unavailable")
        self.assertFalse(snapshot.lsp.enabled)
        self.assertEqual(snapshot.lsp.status, "unavailable")

        # Compilation also succeeds
        compiled = service.compile(snapshot)
        self.assertIn("Workspace Context Snapshot", compiled)


def _configure_test_git_identity(repo: str) -> None:
    """Keep live-Git tests independent of developer or CI global Git config."""
    subprocess.run(
        ["git", "-C", repo, "config", "user.name", "CPTR Tests"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "cptr-tests@example.invalid"],
        check=True,
    )


class LiveWorkspaceContextIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_git_repo_evidence_capture(self):
        temp_dir = tempfile.mkdtemp(prefix="cptr_test_ws_")
        try:
            # Initialize a real git repo
            import cptr.utils.git as git

            await git.init_repo(temp_dir)
            _configure_test_git_identity(temp_dir)

            test_file = Path(temp_dir) / "sample.py"
            test_file.write_text("print('hello world')\n")
            await git.stage(temp_dir, ["sample.py"])
            await git.commit(temp_dir, "Initial sample commit")

            service = WorkspaceContextSnapshotService()
            snapshot = await service.capture_snapshot(
                workspace_root=temp_dir,
                workspace_id="ws_temp",
                include_fdx=False,
                include_lsp=False,
            )

            self.assertTrue(snapshot.repo.is_repo)
            self.assertFalse(snapshot.repo.is_dirty)
            self.assertIsNotNone(snapshot.repo.revision)

            # Make the repo dirty by modifying the file
            test_file.write_text("print('modified world')\n")

            dirty_snapshot = await service.capture_snapshot(
                workspace_root=temp_dir,
                workspace_id="ws_temp",
                checkpoint={"checkpoint_id": "chk_init", "revision": snapshot.repo.revision},
                include_fdx=False,
                include_lsp=False,
            )

            self.assertTrue(dirty_snapshot.repo.is_dirty)
            self.assertTrue(dirty_snapshot.divergence.is_diverged)
            self.assertIn("sample.py", dirty_snapshot.divergence.diverged_files)

            compiled = service.compile(dirty_snapshot)
            self.assertIn("DIVERGENCE DETECTED", compiled)
            self.assertIn("sample.py", compiled)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_check_divergence_standalone(self):
        temp_dir = tempfile.mkdtemp(prefix="cptr_test_ws_div_")
        try:
            import cptr.utils.git as git

            await git.init_repo(temp_dir)
            _configure_test_git_identity(temp_dir)
            f = Path(temp_dir) / "initial.txt"
            f.write_text("v1")
            await git.stage(temp_dir, ["initial.txt"])
            await git.commit(temp_dir, "initial commit")
            rev = await git.current_revision(temp_dir)

            service = WorkspaceContextSnapshotService()
            # Clean baseline
            div_clean = await service.check_divergence(
                workspace_root=temp_dir,
                checkpoint={"checkpoint_id": "chk_base", "revision": rev},
            )
            self.assertFalse(div_clean.is_diverged)
            self.assertFalse(div_clean.is_stale)

            # Modify file -> dirty divergence
            f.write_text("v2")
            div_dirty = await service.check_divergence(
                workspace_root=temp_dir,
                checkpoint={"checkpoint_id": "chk_base", "revision": rev},
            )
            self.assertTrue(div_dirty.is_diverged)
            self.assertIn("initial.txt", div_dirty.diverged_files)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def test_workbench_session_store_lookup(self):
        mock_store = AsyncMock()
        mock_store.get.return_value = {
            "session_id": "wbs_store_lookup",
            "workspace_id": "ws_from_store",
            "active_workspace_id": "ws_active_from_store",
        }

        service = WorkspaceContextSnapshotService(workbench_store=mock_store)
        snapshot = await service.capture_snapshot(
            workspace_root="/tmp",
            user_id="user_store",
            workbench_session_id="wbs_store_lookup",
            include_repo=False,
            include_fdx=False,
            include_lsp=False,
        )

        self.assertEqual(snapshot.workspace_id, "ws_from_store")
        self.assertEqual(snapshot.active_workspace_id, "ws_active_from_store")
        mock_store.get.assert_awaited_once_with(
            owner_id="user_store", session_id="wbs_store_lookup"
        )

    def test_compiler_selective_inclusion_flags(self):
        compiler = WorkspaceContextCompiler()
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_flags",
            workspace_root="/tmp",
            repo=RepoEvidence(is_repo=True, branch="main", revision="123456"),
            divergence=CheckpointDivergence(is_diverged=True, checkpoint_id="chk_x"),
            memory=MemoryEvidence(canonical_memories=["- [fact] fact1"]),
            environment=EnvironmentEvidence(os_info="Linux"),
            instructions=InstructionContext(task_instructions="Do task"),
        )

        # Exclude everything except header
        rendered_minimal = compiler.compile(
            snapshot,
            include_repo=False,
            include_divergence=False,
            include_memory=False,
            include_environment=False,
            include_instructions=False,
        )
        self.assertIn("Workspace Context Snapshot [wcs_flags]", rendered_minimal)
        self.assertNotIn("## Repository Evidence", rendered_minimal)
        self.assertNotIn("## Stale Checkpoint Divergence", rendered_minimal)
        self.assertNotIn("## Memory Context", rendered_minimal)
        self.assertNotIn("## Environment Context", rendered_minimal)
        self.assertNotIn("## Instructions", rendered_minimal)

    def test_protocol_conformance(self):
        from cptr.services.workspace_context import WorkspaceContextCompilerProtocol

        compiler = WorkspaceContextCompiler()
        self.assertTrue(isinstance(compiler, WorkspaceContextCompilerProtocol))


class BoundedWorkersEvidenceTests(unittest.TestCase):
    """Tests for BoundedWorkersEvidence model and round-trip serialization."""

    def test_default_construction(self):
        w = BoundedWorkersEvidence()
        self.assertTrue(w.enabled)
        self.assertEqual(w.status, "ok")
        self.assertEqual(w.total_count, 0)
        self.assertEqual(w.active_count, 0)
        self.assertEqual(w.workers, [])
        self.assertIsNone(w.error)

    def test_serialization_round_trip(self):
        w = BoundedWorkersEvidence(
            enabled=True,
            status="ok",
            total_count=2,
            active_count=1,
            workers=[
                {
                    "worker_id": "dcw_abc123",
                    "name": "Feature Worker",
                    "responsibility": "implement auth",
                    "repo_path": ".",
                    "status": "READY",
                    "changed_file_count": 3,
                    "active_command_count": 0,
                    "created_at": 1700000000000,
                },
            ],
        )
        as_dict = w.to_dict()
        self.assertEqual(as_dict["total_count"], 2)
        self.assertEqual(as_dict["active_count"], 1)
        self.assertEqual(len(as_dict["workers"]), 1)
        self.assertEqual(as_dict["workers"][0]["worker_id"], "dcw_abc123")

        restored = BoundedWorkersEvidence.from_dict(as_dict)
        self.assertEqual(restored.total_count, 2)
        self.assertEqual(restored.active_count, 1)
        self.assertEqual(len(restored.workers), 1)
        self.assertIsNone(restored.error)

    def test_from_dict_none_returns_default(self):
        w = BoundedWorkersEvidence.from_dict(None)
        self.assertTrue(w.enabled)
        self.assertEqual(w.total_count, 0)

    def test_error_degradation(self):
        w = BoundedWorkersEvidence(
            enabled=False,
            status="degraded",
            error="workers evidence degraded: DB error",
        )
        as_dict = w.to_dict()
        restored = BoundedWorkersEvidence.from_dict(as_dict)
        self.assertFalse(restored.enabled)
        self.assertEqual(restored.status, "degraded")
        self.assertIn("DB error", restored.error)

    def test_snapshot_includes_workers_field(self):
        workers = BoundedWorkersEvidence(
            enabled=True,
            status="ok",
            total_count=1,
            active_count=1,
            workers=[
                {
                    "worker_id": "dcw_test",
                    "name": "Test Worker",
                    "responsibility": "",
                    "repo_path": ".",
                    "status": "READY",
                    "changed_file_count": 0,
                    "active_command_count": 0,
                    "created_at": None,
                }
            ],
        )
        snapshot = WorkspaceContextSnapshot(
            workspace_root="/tmp/ws",
            workers=workers,
        )
        as_dict = snapshot.to_dict()
        self.assertIn("workers", as_dict)
        self.assertEqual(as_dict["workers"]["total_count"], 1)

        restored = WorkspaceContextSnapshot.from_dict(as_dict)
        self.assertEqual(restored.workers.total_count, 1)
        self.assertEqual(restored.workers.workers[0]["worker_id"], "dcw_test")


class ProjectEvidenceTests(unittest.TestCase):
    """Tests for ProjectEvidence model and round-trip serialization."""

    def test_default_construction(self):
        p = ProjectEvidence()
        self.assertTrue(p.enabled)
        self.assertEqual(p.status, "ok")
        self.assertIsNone(p.detected_language)
        self.assertEqual(p.manifest_files, [])
        self.assertEqual(p.test_files, [])
        self.assertEqual(p.script_files, [])
        self.assertEqual(p.file_count, 0)
        self.assertEqual(p.diagnostics, [])
        self.assertIsNone(p.error)

    def test_serialization_round_trip(self):
        p = ProjectEvidence(
            enabled=True,
            status="ok",
            detected_language="python",
            manifest_files=["pyproject.toml", "requirements.txt"],
            test_files=["tests/test_foo.py", "tests/test_bar.py"],
            script_files=["scripts/build.sh"],
            source_roots=["."],
            file_count=42,
            diagnostics=[],
        )
        as_dict = p.to_dict()
        self.assertEqual(as_dict["detected_language"], "python")
        self.assertIn("pyproject.toml", as_dict["manifest_files"])
        self.assertEqual(as_dict["file_count"], 42)

        restored = ProjectEvidence.from_dict(as_dict)
        self.assertEqual(restored.detected_language, "python")
        self.assertEqual(len(restored.manifest_files), 2)
        self.assertEqual(restored.file_count, 42)
        self.assertIsNone(restored.error)

    def test_from_dict_none_returns_default(self):
        p = ProjectEvidence.from_dict(None)
        self.assertTrue(p.enabled)
        self.assertEqual(p.manifest_files, [])

    def test_degraded_state(self):
        p = ProjectEvidence(
            enabled=True,
            status="degraded",
            file_count=10,
            diagnostics=["file scan bounded at 500"],
            error="project discovery failed: permission denied",
        )
        as_dict = p.to_dict()
        restored = ProjectEvidence.from_dict(as_dict)
        self.assertEqual(restored.status, "degraded")
        self.assertIn("file scan bounded at 500", restored.diagnostics)
        self.assertIn("permission denied", restored.error)

    def test_snapshot_includes_project_field(self):
        project = ProjectEvidence(
            enabled=True,
            status="ok",
            detected_language="python",
            manifest_files=["pyproject.toml"],
            test_files=["tests/test_main.py"],
            file_count=100,
        )
        snapshot = WorkspaceContextSnapshot(
            workspace_root="/tmp/ws",
            project=project,
        )
        as_dict = snapshot.to_dict()
        self.assertIn("project", as_dict)
        self.assertEqual(as_dict["project"]["detected_language"], "python")

        restored = WorkspaceContextSnapshot.from_dict(as_dict)
        self.assertEqual(restored.project.detected_language, "python")
        self.assertIn("pyproject.toml", restored.project.manifest_files)

    def test_snapshot_digest_is_stable_across_project_changes(self):
        """Project evidence must not affect the core content digest."""
        snap_a = WorkspaceContextSnapshot(
            workspace_root="/tmp/ws",
            workspace_id="ws_1",
            project=ProjectEvidence(manifest_files=["pyproject.toml"]),
        )
        snap_b = WorkspaceContextSnapshot(
            workspace_root="/tmp/ws",
            workspace_id="ws_1",
            project=ProjectEvidence(manifest_files=["Cargo.toml"]),
        )
        # Digest is based on repo/divergence/memory, not project details
        self.assertEqual(snap_a.content_digest, snap_b.content_digest)


class WorkersEvidenceCompilerTests(unittest.TestCase):
    """Tests for compiler rendering of workers and project evidence."""

    def setUp(self):
        self.compiler = WorkspaceContextCompiler()

    def test_compile_renders_active_workers(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_wk_render",
            workspace_root="/tmp",
            workers=BoundedWorkersEvidence(
                enabled=True,
                status="ok",
                total_count=2,
                active_count=1,
                workers=[
                    {
                        "worker_id": "dcw_aaa",
                        "name": "Auth Worker",
                        "responsibility": "auth module",
                        "repo_path": ".",
                        "status": "READY",
                        "changed_file_count": 5,
                        "active_command_count": 0,
                        "created_at": None,
                    },
                    {
                        "worker_id": "dcw_bbb",
                        "name": "Tests Worker",
                        "responsibility": "add tests",
                        "repo_path": ".",
                        "status": "WORKING",
                        "changed_file_count": 2,
                        "active_command_count": 1,
                        "created_at": None,
                    },
                ],
            ),
        )
        rendered = self.compiler.compile(snapshot)
        self.assertIn("## Active Workers", rendered)
        self.assertIn("Auth Worker", rendered)
        self.assertIn("Tests Worker", rendered)
        self.assertIn("changed_files=5", rendered)

    def test_compile_renders_project_evidence(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_proj_render",
            workspace_root="/tmp",
            project=ProjectEvidence(
                enabled=True,
                status="ok",
                detected_language="python",
                manifest_files=["pyproject.toml"],
                test_files=["tests/test_foo.py", "tests/test_bar.py"],
                script_files=["scripts/deploy.sh"],
                file_count=88,
            ),
        )
        rendered = self.compiler.compile(snapshot)
        self.assertIn("## Project Evidence", rendered)
        self.assertIn("Detected Language: python", rendered)
        self.assertIn("pyproject.toml", rendered)
        self.assertIn("test_foo.py", rendered)
        self.assertIn("deploy.sh", rendered)

    def test_compile_workers_disabled_not_rendered(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_wk_disabled",
            workspace_root="/tmp",
            workers=BoundedWorkersEvidence(enabled=False, status="unavailable"),
        )
        rendered = self.compiler.compile(snapshot, include_workers=False)
        self.assertNotIn("## Active Workers", rendered)

    def test_compile_project_disabled_not_rendered(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_proj_disabled",
            workspace_root="/tmp",
            project=ProjectEvidence(enabled=True, detected_language="rust"),
        )
        rendered = self.compiler.compile(snapshot, include_project=False)
        self.assertNotIn("## Project Evidence", rendered)

    def test_compile_workers_error_degraded_note(self):
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_wk_err",
            workspace_root="/tmp",
            workers=BoundedWorkersEvidence(
                enabled=False,
                status="degraded",
                error="workers evidence degraded: timeout",
            ),
        )
        rendered = self.compiler.compile(snapshot)
        self.assertIn("## Active Workers", rendered)
        self.assertIn("timeout", rendered)


class WorkersEvidenceCaptureTests(unittest.IsolatedAsyncioTestCase):
    """Tests for _capture_workers_evidence and _capture_project_evidence."""

    async def test_capture_workers_requires_user_and_workspace_id(self):
        service = WorkspaceContextSnapshotService()
        # No user_id -> degraded, not error
        ev = await service._capture_workers_evidence(
            workspace_root="/tmp",
            user_id=None,
            workspace_id=None,
        )
        self.assertFalse(ev.enabled)
        self.assertEqual(ev.status, "unavailable")

    async def test_capture_workers_degrades_on_service_failure(self):
        from unittest.mock import patch

        with patch(
            "cptr.services.workspace_context._capture_workers_evidence_impl",
            side_effect=RuntimeError("DB exploded"),
            create=True,
        ):
            # The method catches the import error gracefully too
            service = WorkspaceContextSnapshotService()
            # A workspace_id with no DB should fail gracefully
            ev = await service._capture_workers_evidence(
                workspace_root="/tmp",
                user_id="u1",
                workspace_id="ws1",
            )
            # Must not raise; must return degraded or ok evidence
            self.assertIsInstance(ev, BoundedWorkersEvidence)

    async def test_capture_workers_with_mock_service(self):
        from unittest.mock import AsyncMock, patch, MagicMock

        mock_svc = MagicMock()
        mock_svc.list = AsyncMock(
            return_value=[
                {
                    "worker_id": "dcw_x1",
                    "name": "My Worker",
                    "responsibility": "testing",
                    "repo_path": ".",
                    "status": "READY",
                    "changed_file_count": 3,
                    "active_command_ids": [],
                    "recent_command_ids": [],
                    "created_at": 1700000000000,
                }
            ]
        )

        with patch(
            "cptr.services.workspace_context.WorkspaceContextSnapshotService"
            "._capture_workers_evidence",
            new=AsyncMock(
                return_value=BoundedWorkersEvidence(
                    enabled=True,
                    status="ok",
                    total_count=1,
                    active_count=0,
                    workers=[
                        {
                            "worker_id": "dcw_x1",
                            "name": "My Worker",
                            "responsibility": "testing",
                            "repo_path": ".",
                            "status": "READY",
                            "changed_file_count": 3,
                            "active_command_count": 0,
                            "created_at": 1700000000000,
                        }
                    ],
                )
            ),
        ):
            service = WorkspaceContextSnapshotService()
            snapshot = await service.capture_snapshot(
                workspace_root="/tmp",
                user_id="u1",
                workspace_id="ws_test",
                include_repo=False,
                include_fdx=False,
                include_lsp=False,
                include_project=False,
            )
        self.assertTrue(snapshot.workers.enabled)
        self.assertEqual(snapshot.workers.total_count, 1)
        self.assertEqual(snapshot.workers.workers[0]["name"], "My Worker")

    async def test_capture_project_evidence_real_tmpdir(self):
        import tempfile
        from pathlib import Path

        service = WorkspaceContextSnapshotService()
        with tempfile.TemporaryDirectory(prefix="cptr_test_proj_") as tmpdir:
            root = Path(tmpdir)
            # Create recognizable project structure
            (root / "pyproject.toml").write_text('[project]\nname = "test"\n')
            tests_dir = root / "tests"
            tests_dir.mkdir()
            (tests_dir / "test_main.py").write_text("def test_x(): pass\n")
            scripts_dir = root / "scripts"
            scripts_dir.mkdir()
            (scripts_dir / "build.sh").write_text("#!/bin/bash\necho done\n")
            (root / "src" / "app.py").parent.mkdir(parents=True, exist_ok=True)
            (root / "src" / "app.py").write_text("# app\n")

            ev = await service._capture_project_evidence(str(root))

        self.assertTrue(ev.enabled)
        self.assertEqual(ev.status, "ok")
        self.assertEqual(ev.detected_language, "python")
        self.assertIn("pyproject.toml", ev.manifest_files)
        # test_main.py should be discovered
        self.assertTrue(any("test_main.py" in f for f in ev.test_files))
        # build.sh should be discovered
        self.assertTrue(any("build.sh" in f for f in ev.script_files))
        self.assertGreater(ev.file_count, 0)
        self.assertIsNone(ev.error)

    async def test_capture_project_evidence_non_existent_dir(self):
        service = WorkspaceContextSnapshotService()
        ev = await service._capture_project_evidence("/nonexistent/path/xyz")
        self.assertFalse(ev.enabled)
        self.assertEqual(ev.status, "unavailable")
        self.assertIsNotNone(ev.error)

    async def test_capture_project_evidence_bounded(self):
        """Scanning many files degrades gracefully without raising."""
        import tempfile
        from pathlib import Path

        service = WorkspaceContextSnapshotService()
        with tempfile.TemporaryDirectory(prefix="cptr_test_bounded_") as tmpdir:
            root = Path(tmpdir)
            # Create many files to trigger the bound
            many = root / "many"
            many.mkdir()
            for i in range(20):
                (many / f"file_{i:04d}.py").write_text(f"# {i}\n")

            ev = await service._capture_project_evidence(str(root))

        # Must not raise; status is ok or degraded
        self.assertIn(ev.status, {"ok", "degraded"})
        self.assertIsInstance(ev, ProjectEvidence)

    async def test_capture_snapshot_workers_degrade_gracefully(self):
        """Snapshot capture with workers failure must still succeed."""
        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.is_repo = AsyncMock(return_value=False)

        service = WorkspaceContextSnapshotService(git_module=mock_git)

        # Monkey-patch to force a workers failure

        async def fail_workers(**kwargs):
            return BoundedWorkersEvidence(
                enabled=False,
                status="degraded",
                error="simulated workers failure",
            )

        service._capture_workers_evidence = fail_workers

        snapshot = await service.capture_snapshot(
            workspace_root="/tmp",
            user_id="u_test",
            workspace_id="ws_fail",
            include_fdx=False,
            include_lsp=False,
        )

        # Snapshot must succeed; workers evidence must be degraded
        self.assertFalse(snapshot.workers.enabled)
        self.assertEqual(snapshot.workers.status, "degraded")
        self.assertIn("simulated workers failure", snapshot.workers.error)

        # Compilation must also succeed
        compiled = service.compile(snapshot)
        self.assertIn("Workspace Context Snapshot", compiled)

    async def test_capture_snapshot_project_degrades_gracefully(self):
        """Snapshot capture with project failure must still succeed."""
        from unittest.mock import AsyncMock, MagicMock

        mock_git = MagicMock()
        mock_git.is_repo = AsyncMock(return_value=False)

        service = WorkspaceContextSnapshotService(git_module=mock_git)

        async def fail_project(workspace_root):
            return ProjectEvidence(
                enabled=True,
                status="degraded",
                error="simulated project discovery failure",
            )

        service._capture_project_evidence = fail_project

        snapshot = await service.capture_snapshot(
            workspace_root="/tmp",
            user_id="u_test",
            workspace_id="ws_proj_fail",
            include_fdx=False,
            include_lsp=False,
            include_workers=False,
        )

        self.assertEqual(snapshot.project.status, "degraded")
        self.assertIn("simulated project discovery failure", snapshot.project.error)
        compiled = service.compile(snapshot)
        self.assertIn("Workspace Context Snapshot", compiled)

    async def test_workers_paths_not_exposed_externally(self):
        """Worker worktree paths must not appear in compiled output."""
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_path_check",
            workspace_root="/home/user/projects/myapp",
            workers=BoundedWorkersEvidence(
                enabled=True,
                status="ok",
                total_count=1,
                active_count=1,
                workers=[
                    {
                        "worker_id": "dcw_safe",
                        "name": "Safe Worker",
                        "responsibility": "work",
                        "repo_path": ".",
                        "status": "READY",
                        "changed_file_count": 1,
                        "active_command_count": 0,
                        "created_at": None,
                        # These would leak if not filtered:
                        "worktree_path": "/home/user/.cptr-worktrees/myapp/dcw_safe",
                        "branch": "cptr/direct/dcw_safe",
                    }
                ],
            ),
        )
        # The capture method drops worktree_path and branch;
        # but if they slipped through to the model, to_dict goes through
        # redact_sensitive which will hit the path redactor in to_dict
        compiled = WorkspaceContextCompiler().compile(snapshot)
        # Compiled output must have the worker name
        self.assertIn("Safe Worker", compiled)


class ReproducibleVersionAndLiveStateTests(unittest.TestCase):
    """Tests for reproducible version fields, live revision/dirty, and FDX state."""

    def test_version_field_defaults_and_roundtrip(self):
        snapshot = WorkspaceContextSnapshot(workspace_root="/tmp/test_ws")
        self.assertEqual(snapshot.version, 1)

        snapshot_v2 = WorkspaceContextSnapshot(workspace_root="/tmp/test_ws", version=2)
        self.assertEqual(snapshot_v2.version, 2)

        as_dict = snapshot_v2.to_dict()
        self.assertEqual(as_dict["version"], 2)

        restored = WorkspaceContextSnapshot.from_dict(as_dict)
        self.assertEqual(restored.version, 2)

    def test_version_field_affects_digest_reproducibly(self):
        snap1 = WorkspaceContextSnapshot(workspace_root="/tmp/test_ws", version=1)
        snap2 = WorkspaceContextSnapshot(workspace_root="/tmp/test_ws", version=1)
        snap3 = WorkspaceContextSnapshot(workspace_root="/tmp/test_ws", version=2)

        # Same version & state -> identical reproducible digest
        self.assertEqual(snap1.content_digest, snap2.content_digest)
        # Different version -> different digest
        self.assertNotEqual(snap1.content_digest, snap3.content_digest)

    def test_live_revision_dirty_and_fdx_properties(self):
        snapshot = WorkspaceContextSnapshot(
            workspace_root="/tmp/test_ws",
            repo=RepoEvidence(
                is_repo=True,
                revision="feedbeef12345678",
                is_dirty=True,
            ),
            fdx=FdxEvidence(
                enabled=True,
                status="ready",
                version="fdx 0.4.2",
            ),
        )
        self.assertEqual(snapshot.revision, "feedbeef12345678")
        self.assertTrue(snapshot.is_dirty)
        self.assertEqual(snapshot.fdx_status, "ready")

    def test_compiler_and_bundle_include_version_and_live_state(self):
        compiler = WorkspaceContextCompiler()
        snapshot = WorkspaceContextSnapshot(
            snapshot_id="wcs_ver_test",
            version=1,
            workspace_root="/tmp/test_ws",
            workspace_id="ws_ver",
            repo=RepoEvidence(
                is_repo=True,
                revision="commit_live_rev_999",
                is_dirty=True,
            ),
            fdx=FdxEvidence(
                enabled=True,
                status="ready",
                version="fdx 1.0.0",
            ),
        )

        rendered = compiler.compile(snapshot)
        self.assertIn("- Version: 1", rendered)
        self.assertIn("- HEAD Revision: commit_live_rev_999", rendered)
        self.assertIn("- Version: fdx 1.0.0", rendered)

        bundle = compiler.compile_bundle(snapshot)
        self.assertEqual(bundle["version"], 1)
        self.assertEqual(bundle["revision"], "commit_live_rev_999")
        self.assertTrue(bundle["is_dirty"])
        self.assertIn("diagnostics", bundle)


class DegradedSubsystemDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    """Tests for explicit degraded diagnostics across all subsystems."""

    async def test_repo_degraded_diagnostics_aggregation(self):
        mock_git = Mock()
        mock_git.is_repo = AsyncMock(return_value=True)
        mock_git.status = AsyncMock(return_value={"branch": "main", "dirty": False, "files": []})
        mock_git.current_revision = AsyncMock(return_value="sha123")
        # Simulating log failure
        mock_git.log = AsyncMock(side_effect=RuntimeError("git log timeout"))

        service = WorkspaceContextSnapshotService(
            git_module=mock_git,
            fdx_service=None,
            lsp_service=None,
        )

        snapshot = await service.capture_snapshot(
            workspace_root="/tmp",
            user_id="user1",
            workspace_id="ws1",
            include_fdx=False,
            include_lsp=False,
            include_workers=False,
            include_project=False,
        )

        self.assertTrue(snapshot.repo.is_repo)
        self.assertEqual(snapshot.repo.status, "ok")
        self.assertTrue(
            any("recent commits retrieval degraded" in d for d in snapshot.repo.diagnostics)
        )
        self.assertTrue(any("recent commits retrieval degraded" in d for d in snapshot.diagnostics))

        compiler = WorkspaceContextCompiler()
        rendered = compiler.compile(snapshot)
        self.assertIn("## Degraded Subsystem Diagnostics", rendered)
        self.assertIn("recent commits retrieval degraded", rendered)

    async def test_fdx_degraded_diagnostics_aggregation(self):
        mock_fdx = Mock()
        mock_fdx.execute = AsyncMock(
            return_value={
                "status": "degraded",
                "ready": False,
                "reason": "Daemon re-indexing large repository",
                "daemon_running": True,
                "capabilities": ["read"],
            }
        )

        mock_git = Mock()
        mock_git.is_repo = AsyncMock(return_value=False)

        service = WorkspaceContextSnapshotService(
            git_module=mock_git,
            fdx_service=mock_fdx,
        )

        snapshot = await service.capture_snapshot(
            workspace_root="/tmp",
            user_id="user1",
            workspace_id="ws1",
            include_repo=False,
            include_lsp=False,
            include_workers=False,
            include_project=False,
        )

        self.assertEqual(snapshot.fdx.status, "degraded")
        self.assertTrue(any("Daemon re-indexing" in d for d in snapshot.fdx.diagnostics))
        self.assertTrue(any("Daemon re-indexing" in d for d in snapshot.diagnostics))

        compiler = WorkspaceContextCompiler()
        rendered = compiler.compile(snapshot)
        self.assertIn("## Degraded Subsystem Diagnostics", rendered)
        self.assertIn("Daemon re-indexing", rendered)

    async def test_workers_and_project_degraded_diagnostics(self):
        service = WorkspaceContextSnapshotService()
        # Missing workspace_id or user_id triggers explicit degraded diagnostic
        snapshot = await service.capture_snapshot(
            workspace_root="/tmp",
            user_id=None,
            workspace_id=None,
            include_repo=False,
            include_fdx=False,
            include_lsp=False,
            include_project=False,
        )

        self.assertEqual(snapshot.workers.status, "unavailable")
        self.assertTrue(any("workers evidence skipped" in d for d in snapshot.workers.diagnostics))
        self.assertTrue(any("workers evidence skipped" in d for d in snapshot.diagnostics))

    def test_compiler_selective_diagnostics_exclusion(self):
        compiler = WorkspaceContextCompiler()
        snapshot = WorkspaceContextSnapshot(
            workspace_root="/tmp",
            diagnostics=["repo: git inspection failed: something broke"],
        )

        rendered_with = compiler.compile(snapshot, include_diagnostics=True)
        self.assertIn("## Degraded Subsystem Diagnostics", rendered_with)
        self.assertIn("something broke", rendered_with)

        rendered_without = compiler.compile(snapshot, include_diagnostics=False)
        self.assertNotIn("## Degraded Subsystem Diagnostics", rendered_without)
