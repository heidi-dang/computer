"""Regression tests: alias-compatibility gaps in workspace-ownership checks.

Exercises every mutation that previously did a raw workspace-string comparison
(supersede_memory, verify_memory, get_snapshot / create_branch, begin_forget /
forget_memory, mark_disputed, supersede_with_existing) and confirms that:

  - A record stored under the legacy path "/repo" is mutable via the stable
    UUID "workspace-1" (and vice-versa).
  - A different user cannot perform those mutations even when they supply a
    matching workspace identifier.
  - The centralized WorkspaceNamespace resolver is the sole gate; no ad-hoc
    string comparison slips through.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.utils.memory import MemoryRoot, apply_markdown_memory_batch, remember
from cptr.memory.domain import ManagedContext, MemoryReplacement
from cptr.memory.service import EmbeddedMemoryService
from cptr.memory.store import SqlMemoryStore
from cptr.models import Base, User, Workspace
from cptr.services.memory_fabric import MemoryFabricStore


class LegacyMemorySetTests(unittest.IsolatedAsyncioTestCase):
    def test_markdown_set_replaces_file_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root_path = Path(tmp)
            baseline = root_path / "MEMORY.md"
            baseline.write_text("old content\n")
            root = MemoryRoot("user", root_path, baseline)

            result = apply_markdown_memory_batch(
                root,
                [{"action": "set", "path": "MEMORY.md", "content": "new durable fact"}],
            )

            self.assertTrue(result["success"])
            self.assertEqual(baseline.read_text(), "new durable fact\n")

    async def test_remember_normalizes_legacy_type_set_and_queues_canonical_memory(self):
        request = SimpleNamespace()
        service = SimpleNamespace(queue_consolidation=AsyncMock(return_value="job-1"))
        with (
            patch(
                "cptr.utils.memory.get_memory_settings",
                new=AsyncMock(return_value={"enabled": True}),
            ),
            patch(
                "cptr.utils.memory.write_memory",
                new=AsyncMock(
                    return_value={
                        "success": True,
                        "message": "set memory file MEMORY.md",
                        "path": "/memory",
                    }
                ),
            ) as write_memory,
            patch(
                "cptr.utils.memory._record_memory_fabric_event",
                new=AsyncMock(return_value="event-1"),
            ),
            patch("cptr.memory.service.get_memory_service", return_value=service),
        ):
            result = await remember(
                request,
                user_id="user-1",
                workspace="/repo",
                scope="workspace",
                operations=[{"type": "set", "path": "MEMORY.md", "content": "durable fact"}],
            )

        self.assertTrue(result["success"])
        normalized = write_memory.await_args.args[4]
        self.assertEqual(normalized[0]["action"], "set")
        service.queue_consolidation.assert_awaited_once()
        self.assertEqual(service.queue_consolidation.await_args.kwargs["text"], "durable fact")
        self.assertEqual(
            service.queue_consolidation.await_args.kwargs["source_event_ids"], ["event-1"]
        )


# ─────────────────────────────────────────────────────────────────────────────
# Shared setup helper
# ─────────────────────────────────────────────────────────────────────────────


class _AliasBase(unittest.IsolatedAsyncioTestCase):
    """Create a workspace with id="workspace-1", path="/repo", name="Repo".

    The workspace resolver will therefore accept all three strings
    ("workspace-1", "/repo", "Repo") as aliases for each other.
    """

    WORKSPACE_ID = "workspace-1"
    WORKSPACE_PATH = "/repo"
    WORKSPACE_NAME = "Repo"
    USER_ID = "user-1"
    OTHER_USER = "user-2"

    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            db.add(User(id=self.USER_ID, role="admin", settings={}, created_at=1))
            db.add(User(id=self.OTHER_USER, role="user", settings={}, created_at=1))
            db.add(
                Workspace(
                    id=self.WORKSPACE_ID,
                    user_id=self.USER_ID,
                    path=self.WORKSPACE_PATH,
                    name=self.WORKSPACE_NAME,
                    data={},
                    created_at=1,
                    updated_at=1,
                )
            )
            await db.commit()
        self.store = SqlMemoryStore(session_factory=self.sessions)
        self.events = MemoryFabricStore(session_factory=self.sessions)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    def _service(self) -> EmbeddedMemoryService:
        return EmbeddedMemoryService(
            store=self.store,
            event_store=self.events,
            managed_context_loader=AsyncMock(return_value=ManagedContext(rendered="", items=[])),
            settings_loader=AsyncMock(
                return_value={
                    "enabled": True,
                    "required_for_execution": False,
                    "verification_ttl_seconds": 86400,
                }
            ),
        )

    async def _memory(self, workspace: str = WORKSPACE_PATH) -> str:
        """Create a minimal active memory in *workspace* and return its id."""
        ref = await self.store.create_memory(
            user_id=self.USER_ID,
            workspace=workspace,
            scope="workspace",
            kind="semantic",
            canonical_text="The deployment target is production.",
            trust_level="agent_observation",
        )
        return ref.memory_id


# ─────────────────────────────────────────────────────────────────────────────
# 1. verify_memory
# ─────────────────────────────────────────────────────────────────────────────


class VerifyMemoryAliasTests(_AliasBase):
    """verify_memory must accept UUID when record stored under legacy path."""

    async def test_verify_via_uuid_when_stored_under_path(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_PATH)
        # Stored under "/repo"; caller supplies stable UUID "workspace-1".
        row = await self.store.verify_memory(
            memory_id=memory_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_ID,  # UUID alias
            verified_at_ms=9_000_000,
            verification_expires_at_ms=9_086_400_000,
            confidence_ppm=980_000,
        )
        self.assertIsNotNone(row["verified_at_ms"])

    async def test_verify_via_path_when_stored_under_uuid(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_ID)
        # Stored under UUID; caller supplies legacy path.
        row = await self.store.verify_memory(
            memory_id=memory_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_PATH,  # path alias
            verified_at_ms=9_000_001,
            verification_expires_at_ms=None,
            confidence_ppm=900_000,
        )
        self.assertIsNotNone(row["verified_at_ms"])

    async def test_verify_rejected_for_wrong_user(self) -> None:
        memory_id = await self._memory()
        with self.assertRaises(KeyError):
            await self.store.verify_memory(
                memory_id=memory_id,
                user_id=self.OTHER_USER,  # wrong owner
                workspace=self.WORKSPACE_PATH,
                verified_at_ms=1,
                verification_expires_at_ms=None,
                confidence_ppm=900_000,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 2. supersede_memory (via service.supersede for full stack coverage)
# ─────────────────────────────────────────────────────────────────────────────


class SupersedeMemoryAliasTests(_AliasBase):
    async def test_supersede_via_uuid_when_stored_under_path(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_PATH)
        service = self._service()
        ref = await service.supersede(
            memory_id,
            MemoryReplacement(
                user_id=self.USER_ID,
                workspace=self.WORKSPACE_ID,  # UUID alias
                scope="workspace",
                kind="semantic",
                canonical_text="The deployment target is staging.",
                trust_level="agent_observation",
                valid_from_ms=5_000,
                source_event_ids=[],
            ),
        )
        self.assertIsNotNone(ref.memory_id)
        old = await self.store.get_memory(memory_id)
        self.assertEqual(old["status"], "superseded")

    async def test_supersede_via_path_when_stored_under_uuid(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_ID)
        service = self._service()
        ref = await service.supersede(
            memory_id,
            MemoryReplacement(
                user_id=self.USER_ID,
                workspace=self.WORKSPACE_PATH,  # path alias
                scope="workspace",
                kind="semantic",
                canonical_text="The deployment target is canary.",
                trust_level="agent_observation",
                valid_from_ms=6_000,
                source_event_ids=[],
            ),
        )
        self.assertIsNotNone(ref.memory_id)
        old = await self.store.get_memory(memory_id)
        self.assertEqual(old["status"], "superseded")

    async def test_supersede_rejected_for_wrong_user(self) -> None:
        memory_id = await self._memory()
        with self.assertRaises(KeyError):
            await self.store.supersede_memory(
                old_memory_id=memory_id,
                user_id=self.OTHER_USER,
                workspace=self.WORKSPACE_PATH,
                scope="workspace",
                kind="semantic",
                canonical_text="Hijacked.",
                structured_value={},
                source_event_ids=[],
                trust_level="agent_observation",
                confidence_ppm=900_000,
                importance_ppm=500_000,
                valid_from_ms=1,
                verification_expires_at_ms=None,
                branch_id=None,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 3. begin_forget / forget_memory
# ─────────────────────────────────────────────────────────────────────────────


class ForgetMemoryAliasTests(_AliasBase):
    async def test_begin_forget_via_uuid_when_stored_under_path(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_PATH)
        row = await self.store.begin_forget(
            memory_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_ID,  # UUID alias
        )
        self.assertEqual(row["status"], "deleting")

    async def test_begin_forget_via_path_when_stored_under_uuid(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_ID)
        row = await self.store.begin_forget(
            memory_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_PATH,  # path alias
        )
        self.assertEqual(row["status"], "deleting")

    async def test_begin_forget_rejected_for_wrong_user(self) -> None:
        memory_id = await self._memory()
        with self.assertRaises(KeyError):
            await self.store.begin_forget(
                memory_id,
                user_id=self.OTHER_USER,
                workspace=self.WORKSPACE_PATH,
            )

    async def test_forget_memory_via_uuid_when_stored_under_path(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_PATH)
        # forget_memory does a hard delete; it should accept alias
        await self.store.forget_memory(
            memory_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_ID,  # UUID alias
        )
        with self.assertRaises(KeyError):
            await self.store.get_memory(memory_id)

    async def test_forget_memory_rejected_for_wrong_user(self) -> None:
        memory_id = await self._memory()
        with self.assertRaises(KeyError):
            await self.store.forget_memory(
                memory_id,
                user_id=self.OTHER_USER,
                workspace=self.WORKSPACE_PATH,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 4. mark_disputed
# ─────────────────────────────────────────────────────────────────────────────


class MarkDisputedAliasTests(_AliasBase):
    async def test_mark_disputed_via_uuid_when_stored_under_path(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_PATH)
        await self.store.mark_disputed(
            memory_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_ID,  # UUID alias
        )
        row = await self.store.get_memory(memory_id)
        self.assertEqual(row["status"], "disputed")

    async def test_mark_disputed_via_path_when_stored_under_uuid(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_ID)
        await self.store.mark_disputed(
            memory_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_PATH,  # path alias
        )
        row = await self.store.get_memory(memory_id)
        self.assertEqual(row["status"], "disputed")

    async def test_mark_disputed_rejected_for_wrong_user(self) -> None:
        memory_id = await self._memory()
        with self.assertRaises(KeyError):
            await self.store.mark_disputed(
                memory_id,
                user_id=self.OTHER_USER,
                workspace=self.WORKSPACE_PATH,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 5. get_snapshot / create_branch (ownership check on snapshot)
# ─────────────────────────────────────────────────────────────────────────────


class SnapshotAliasTests(_AliasBase):
    async def _snapshot(self, workspace: str) -> str:
        ref = await self.store.create_snapshot(self.USER_ID, workspace, label="test snap")
        return ref.snapshot_id

    async def test_get_snapshot_via_uuid_when_stored_under_path(self) -> None:
        snap_id = await self._snapshot(self.WORKSPACE_PATH)
        snap = await self.store.get_snapshot(
            self.USER_ID,
            self.WORKSPACE_ID,
            snap_id,  # UUID alias
        )
        self.assertEqual(snap["snapshot_id"], snap_id)

    async def test_get_snapshot_via_path_when_stored_under_uuid(self) -> None:
        snap_id = await self._snapshot(self.WORKSPACE_ID)
        snap = await self.store.get_snapshot(
            self.USER_ID,
            self.WORKSPACE_PATH,
            snap_id,  # path alias
        )
        self.assertEqual(snap["snapshot_id"], snap_id)

    async def test_get_snapshot_rejected_for_wrong_user(self) -> None:
        snap_id = await self._snapshot(self.WORKSPACE_PATH)
        with self.assertRaises(KeyError):
            await self.store.get_snapshot(self.OTHER_USER, self.WORKSPACE_PATH, snap_id)

    async def test_create_branch_from_snapshot_via_uuid_when_snap_stored_under_path(
        self,
    ) -> None:
        snap_id = await self._snapshot(self.WORKSPACE_PATH)
        ref = await self.store.create_branch(
            self.USER_ID,
            self.WORKSPACE_ID,  # UUID alias
            name="exp",
            from_snapshot_id=snap_id,
        )
        self.assertIsNotNone(ref.branch_id)

    async def test_create_branch_from_snapshot_rejected_for_wrong_user(self) -> None:
        snap_id = await self._snapshot(self.WORKSPACE_PATH)
        with self.assertRaises(KeyError):
            await self.store.create_branch(
                self.OTHER_USER,
                self.WORKSPACE_PATH,
                name="evil",
                from_snapshot_id=snap_id,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 6. supersede_with_existing
# ─────────────────────────────────────────────────────────────────────────────


class SupersedeWithExistingAliasTests(_AliasBase):
    async def test_supersede_with_existing_uuid_cross_alias(self) -> None:
        old_id = await self._memory(workspace=self.WORKSPACE_PATH)
        new_id = await self._memory(workspace=self.WORKSPACE_PATH)
        # Caller supplies UUID alias; records stored under path — must succeed.
        await self.store.supersede_with_existing(
            old_memory_id=old_id,
            new_memory_id=new_id,
            user_id=self.USER_ID,
            workspace=self.WORKSPACE_ID,  # UUID alias
        )
        old = await self.store.get_memory(old_id)
        self.assertEqual(old["status"], "superseded")

    async def test_supersede_with_existing_rejected_for_wrong_user(self) -> None:
        old_id = await self._memory()
        new_id = await self._memory()
        with self.assertRaises(KeyError):
            await self.store.supersede_with_existing(
                old_memory_id=old_id,
                new_memory_id=new_id,
                user_id=self.OTHER_USER,
                workspace=self.WORKSPACE_PATH,
            )


# ─────────────────────────────────────────────────────────────────────────────
# 7. service.inspect / project_graph alias compat
# ─────────────────────────────────────────────────────────────────────────────


class InspectAliasTests(_AliasBase):
    async def test_inspect_via_uuid_when_stored_under_path(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_PATH)
        service = self._service()
        row = await service.inspect(memory_id, user_id=self.USER_ID, workspace=self.WORKSPACE_ID)
        self.assertEqual(row["memory_id"], memory_id)

    async def test_inspect_via_path_when_stored_under_uuid(self) -> None:
        memory_id = await self._memory(workspace=self.WORKSPACE_ID)
        service = self._service()
        row = await service.inspect(memory_id, user_id=self.USER_ID, workspace=self.WORKSPACE_PATH)
        self.assertEqual(row["memory_id"], memory_id)

    async def test_inspect_rejected_for_wrong_user(self) -> None:
        memory_id = await self._memory()
        service = self._service()
        with self.assertRaises(KeyError):
            await service.inspect(memory_id, user_id=self.OTHER_USER, workspace=self.WORKSPACE_PATH)


if __name__ == "__main__":
    unittest.main()
