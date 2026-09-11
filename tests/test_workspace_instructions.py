"""Tests for versioned Workspace instructions.

Covers:
- WorkspaceInstructionVersion model and serialization
- Safe lookup and current version pointer resolution
- Content hash computation (SHA-256)
- Optimistic concurrency with expected_version
- Version history and specific version lookup
- Owner scoping and permission boundaries
- Compiled preview generation and fallback without a second instruction engine
- load_system_prompt integration
- REST API router endpoints
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, User, Workspace, WorkspaceInstructionVersion
from cptr.routers.workspace_instructions import router as workspace_instructions_router
from cptr.services.workspace_instructions import (
    WorkspaceAccessDeniedError,
    WorkspaceInstructionConflictError,
    WorkspaceInstructionService,
    WorkspaceNotFoundError,
    compute_content_hash,
)
from cptr.utils.prompt_templates import load_system_prompt


class WorkspaceInstructionsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_instructions.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

        async with self.sessions() as db:
            db.add_all(
                [
                    User(id="user-alice", role="admin", settings={}, created_at=1),
                    User(id="user-bob", role="user", settings={}, created_at=1),
                ]
            )
            self.ws_alice_path = str(Path(self.temp_dir.name) / "alice_ws")
            Path(self.ws_alice_path).mkdir(parents=True, exist_ok=True)

            self.ws_bob_path = str(Path(self.temp_dir.name) / "bob_ws")
            Path(self.ws_bob_path).mkdir(parents=True, exist_ok=True)

            self.ws_alice = Workspace(
                id="ws-alice-1",
                user_id="user-alice",
                path=self.ws_alice_path,
                name="Alice Workspace",
                slug="alice-ws",
                workspace_type="project",
                data={},
                created_at=1,
                updated_at=1,
            )
            self.ws_bob = Workspace(
                id="ws-bob-1",
                user_id="user-bob",
                path=self.ws_bob_path,
                name="Bob Workspace",
                slug="bob-ws",
                workspace_type="project",
                data={},
                created_at=1,
                updated_at=1,
            )
            db.add_all([self.ws_alice, self.ws_bob])
            await db.commit()

        self.service = WorkspaceInstructionService()
        self._patches = [
            patch("cptr.utils.db.get_db", new=self._db),
            patch("cptr.models.workspaces.get_db", new=self._db),
            patch("cptr.services.workspace_refs.get_db", new=self._db),
            patch("cptr.services.workspace_instructions.get_db", new=self._db),
        ]
        for p in self._patches:
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self._patches):
            p.stop()
        await self.engine.dispose()
        self.temp_dir.cleanup()

    async def _db(self):
        return self.sessions()

    # ──────────────────────────────────────────────────────────────────────────
    # Model & Content Hashing
    # ──────────────────────────────────────────────────────────────────────────

    async def test_content_hash_and_model_serialization(self):
        content = "# Alice Instructions\nAlways run tests before committing."
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.assertEqual(compute_content_hash(content), expected_hash)

        version_record = WorkspaceInstructionVersion(
            id="ver-1",
            workspace_id="ws-alice-1",
            user_id="user-alice",
            version=1,
            content=content,
            content_hash=expected_hash,
            is_current=True,
            change_summary="Initial commit",
            created_at=1000,
        )
        d = version_record.to_dict()
        self.assertEqual(d["id"], "ver-1")
        self.assertEqual(d["workspace_id"], "ws-alice-1")
        self.assertEqual(d["user_id"], "user-alice")
        self.assertEqual(d["version"], 1)
        self.assertEqual(d["content_hash"], expected_hash)
        self.assertTrue(d["is_current"])
        self.assertEqual(d["change_summary"], "Initial commit")
        self.assertEqual(d["created_at"], 1000)

    # ──────────────────────────────────────────────────────────────────────────
    # Initial Save & Version Pointer
    # ──────────────────────────────────────────────────────────────────────────

    async def test_initial_save_with_optimistic_concurrency_and_pointer(self):
        # Fresh workspace has no instructions
        current = await self.service.get_current_instruction(
            user_id="user-alice",
            workspace_id="ws-alice-1",
        )
        self.assertIsNone(current)

        # Saving first version with expected_version=0
        v1 = await self.service.save_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            content="Use Python 3.14 conventions.",
            expected_version=0,
            change_summary="Initial setup",
        )
        self.assertEqual(v1.version, 1)
        self.assertTrue(v1.is_current)
        self.assertEqual(v1.content, "Use Python 3.14 conventions.")
        self.assertEqual(
            v1.content_hash,
            compute_content_hash("Use Python 3.14 conventions."),
        )

        # Workspace pointer must be updated
        async with self.sessions() as db:
            ws = await db.get(Workspace, "ws-alice-1")
            self.assertEqual(ws.current_instruction_version_id, v1.id)

        # Safe lookup retrieves v1
        current = await self.service.get_current_instruction(
            user_id="user-alice",
            workspace_id="ws-alice-1",
        )
        self.assertIsNotNone(current)
        self.assertEqual(current.id, v1.id)
        self.assertEqual(current.version, 1)

    # ──────────────────────────────────────────────────────────────────────────
    # Version Progression & Optimistic Concurrency
    # ──────────────────────────────────────────────────────────────────────────

    async def test_version_progression_and_conflict_rejection(self):
        # Create v1
        v1 = await self.service.save_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            content="v1 instructions",
            expected_version=0,
        )
        self.assertEqual(v1.version, 1)

        # Create v2 with matching expected_version=1
        v2 = await self.service.save_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            content="v2 instructions",
            expected_version=1,
            change_summary="Update to v2",
        )
        self.assertEqual(v2.version, 2)
        self.assertTrue(v2.is_current)

        # Verify v1 is no longer current in DB
        async with self.sessions() as db:
            v1_db = await db.get(WorkspaceInstructionVersion, v1.id)
            self.assertFalse(v1_db.is_current)
            ws = await db.get(Workspace, "ws-alice-1")
            self.assertEqual(ws.current_instruction_version_id, v2.id)

        # Attempt update with stale expected_version=1 must fail
        with self.assertRaises(WorkspaceInstructionConflictError) as ctx:
            await self.service.save_instruction_version(
                user_id="user-alice",
                workspace_id="ws-alice-1",
                content="stale concurrent update",
                expected_version=1,
            )
        self.assertEqual(ctx.exception.current_version, 2)
        self.assertEqual(ctx.exception.expected_version, 1)
        self.assertEqual(ctx.exception.workspace_id, "ws-alice-1")

        # Attempt update with expected_version=0 on existing instructions must fail
        with self.assertRaises(WorkspaceInstructionConflictError) as ctx:
            await self.service.save_instruction_version(
                user_id="user-alice",
                workspace_id="ws-alice-1",
                content="bad reset",
                expected_version=0,
            )
        self.assertEqual(ctx.exception.current_version, 2)

        # State is unchanged after conflict
        current = await self.service.get_current_instruction(
            user_id="user-alice",
            workspace_id="ws-alice-1",
        )
        self.assertEqual(current.version, 2)
        self.assertEqual(current.content, "v2 instructions")

    # ──────────────────────────────────────────────────────────────────────────
    # Safe Lookup Resilience
    # ──────────────────────────────────────────────────────────────────────────

    async def test_safe_lookup_falls_back_gracefully_when_pointer_is_cleared(self):
        v1 = await self.service.save_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            content="resilient instructions",
            expected_version=0,
        )

        # Clear pointer on workspace row
        async with self.sessions() as db:
            ws = await db.get(Workspace, "ws-alice-1")
            ws.current_instruction_version_id = None
            await db.commit()

        # Safe lookup still finds the active instruction via is_current flag
        current = await self.service.get_current_instruction(
            user_id="user-alice",
            workspace_id="ws-alice-1",
        )
        self.assertIsNotNone(current)
        self.assertEqual(current.id, v1.id)

    # ──────────────────────────────────────────────────────────────────────────
    # History & Specific Version Retrieval
    # ──────────────────────────────────────────────────────────────────────────

    async def test_history_and_specific_version_retrieval(self):
        for i in range(1, 4):
            await self.service.save_instruction_version(
                user_id="user-alice",
                workspace_id="ws-alice-1",
                content=f"version {i} text",
                expected_version=i - 1,
                change_summary=f"step {i}",
            )

        # History ordering (descending)
        history, total = await self.service.get_instruction_history(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            limit=10,
            offset=0,
        )
        self.assertEqual(total, 3)
        self.assertEqual(len(history), 3)
        self.assertEqual([h.version for h in history], [3, 2, 1])

        # History pagination
        page1, total = await self.service.get_instruction_history(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            limit=2,
            offset=0,
        )
        self.assertEqual(len(page1), 2)
        self.assertEqual([h.version for h in page1], [3, 2])

        # Specific version lookup
        v1_fetch = await self.service.get_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            version=1,
        )
        self.assertIsNotNone(v1_fetch)
        self.assertEqual(v1_fetch.version, 1)
        self.assertEqual(v1_fetch.content, "version 1 text")

        # Non-existent version returns None
        self.assertIsNone(
            await self.service.get_instruction_version(
                user_id="user-alice",
                workspace_id="ws-alice-1",
                version=99,
            )
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Owner Scoping & Isolation
    # ──────────────────────────────────────────────────────────────────────────

    async def test_owner_scoping_enforcement(self):
        # Alice creates instructions
        await self.service.save_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            content="secret alice rules",
            expected_version=0,
        )

        # Bob cannot read Alice's instructions
        with self.assertRaises(WorkspaceAccessDeniedError):
            await self.service.get_current_instruction(
                user_id="user-bob",
                workspace_id="ws-alice-1",
            )

        # Bob cannot write to Alice's instructions
        with self.assertRaises(WorkspaceAccessDeniedError):
            await self.service.save_instruction_version(
                user_id="user-bob",
                workspace_id="ws-alice-1",
                content="malicious overwrite",
                expected_version=1,
            )

        # Bob cannot view Alice's history
        with self.assertRaises(WorkspaceAccessDeniedError):
            await self.service.get_instruction_history(
                user_id="user-bob",
                workspace_id="ws-alice-1",
            )

        # Bob cannot view Alice's version
        with self.assertRaises(WorkspaceAccessDeniedError):
            await self.service.get_instruction_version(
                user_id="user-bob",
                workspace_id="ws-alice-1",
                version=1,
            )

        # Non-existent workspace raises WorkspaceNotFoundError
        with self.assertRaises(WorkspaceNotFoundError):
            await self.service.get_current_instruction(
                user_id="user-alice",
                workspace_id="non-existent-ws",
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Compiled Preview & Canonical Prompt Formatting
    # ──────────────────────────────────────────────────────────────────────────

    async def test_compiled_preview_canonical_formatting_and_fallback(self):
        # 1. Preview with candidate text (no mutation)
        candidate_preview = await self.service.compile_preview(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            candidate_content="Strict typing required.",
        )
        self.assertEqual(candidate_preview.source, "candidate")
        self.assertIn(
            "<instructions>\nStrict typing required.\n</instructions>",
            candidate_preview.compiled_instructions,
        )
        self.assertGreater(candidate_preview.estimated_tokens, 0)
        self.assertEqual(candidate_preview.char_count, len("Strict typing required."))

        # 2. Preview with filesystem fallback when no DB version exists
        memory_md = Path(self.ws_alice_path) / "MEMORY.md"
        memory_md.write_text("Use uv for python packages.")
        file_fallback_preview = await self.service.compile_preview(
            user_id="user-alice",
            workspace_id="ws-alice-1",
        )
        self.assertEqual(file_fallback_preview.source, "instruction_files")
        self.assertIn("# MEMORY.md", file_fallback_preview.content)
        self.assertIn("Use uv for python packages.", file_fallback_preview.compiled_instructions)

        # 3. Save DB instruction version -> takes precedence over filesystem
        await self.service.save_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            content="Canonical DB Instructions v1.",
            expected_version=0,
        )
        db_preview = await self.service.compile_preview(
            user_id="user-alice",
            workspace_id="ws-alice-1",
        )
        self.assertEqual(db_preview.source, "workspace_instruction")
        self.assertEqual(db_preview.version, 1)
        self.assertIn("Canonical DB Instructions v1.", db_preview.compiled_instructions)
        self.assertIn("(v1)", db_preview.compiled_instructions)

    # ──────────────────────────────────────────────────────────────────────────
    # System Prompt Integration
    # ──────────────────────────────────────────────────────────────────────────

    async def test_load_system_prompt_includes_versioned_instructions(self):
        await self.service.save_instruction_version(
            user_id="user-alice",
            workspace_id="ws-alice-1",
            content="Strict commit rules for Alice.",
            expected_version=0,
        )

        async def fake_prepare_context(val):
            bundle = MagicMock()
            bundle.rendered = ""
            return bundle

        mock_svc = MagicMock()
        mock_svc.prepare_context = fake_prepare_context

        with patch("cptr.memory.service.get_memory_service", return_value=mock_svc):
            mock_request = MagicMock()
            prompt = await load_system_prompt(
                request=mock_request,
                workspace=self.ws_alice_path,
                model="gpt-5-preview",
                user_id="user-alice",
            )
            self.assertIn("<instructions>", prompt)
            self.assertIn("Strict commit rules for Alice.", prompt)
            self.assertIn("(v1)", prompt)

    # ──────────────────────────────────────────────────────────────────────────
    # REST API Router
    # ──────────────────────────────────────────────────────────────────────────

    async def test_router_endpoints(self):
        app = FastAPI()
        app.include_router(workspace_instructions_router)

        # Mock authentication state
        async def mock_auth_middleware(request, call_next):
            request.state.auth = MagicMock(user_id="user-alice")
            return await call_next(request)

        app.middleware("http")(mock_auth_middleware)

        client = TestClient(app)

        # 1. GET initial instructions (None)
        res = client.get("/api/workspaces/ws-alice-1/instructions")
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["current"])

        # 2. PUT create version 1
        res = client.put(
            "/api/workspaces/ws-alice-1/instructions",
            json={
                "content": "API defined instructions",
                "expected_version": 0,
                "change_summary": "Created via API",
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["version"]["version"], 1)
        self.assertEqual(data["version"]["content"], "API defined instructions")

        # 3. GET current instructions
        res = client.get("/api/workspaces/ws-alice-1/instructions")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["current"]["version"], 1)

        # 4. PUT optimistic concurrency conflict
        res = client.put(
            "/api/workspaces/ws-alice-1/instructions",
            json={
                "content": "Conflicting instructions",
                "expected_version": 0,
            },
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["detail"]["code"], "INSTRUCTION_VERSION_CONFLICT")
        self.assertEqual(res.json()["detail"]["current_version"], 1)
        self.assertEqual(res.json()["detail"]["expected_version"], 0)

        # 5. PUT valid update to version 2
        res = client.put(
            "/api/workspaces/ws-alice-1/instructions",
            json={
                "content": "API defined instructions v2",
                "expected_version": 1,
            },
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["version"]["version"], 2)

        # 6. GET history
        res = client.get("/api/workspaces/ws-alice-1/instructions/history")
        self.assertEqual(res.status_code, 200)
        history = res.json()
        self.assertEqual(history["total"], 2)
        self.assertEqual(len(history["items"]), 2)

        # 7. GET specific version
        res = client.get("/api/workspaces/ws-alice-1/instructions/versions/1")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["version"]["version"], 1)

        # 8. POST preview
        res = client.post(
            "/api/workspaces/ws-alice-1/instructions/preview",
            json={"content": "Candidate preview test"},
        )
        self.assertEqual(res.status_code, 200)
        preview = res.json()["preview"]
        self.assertEqual(preview["source"], "candidate")
        self.assertIn("Candidate preview test", preview["compiled_instructions"])


if __name__ == "__main__":
    unittest.main()
