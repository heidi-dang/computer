from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base, Chat, User, Workspace
from cptr.routers.chat import SendMessageRequest, _resolve_chat_workspace, send_message, fork_chat
from cptr.utils.prompt_templates import load_system_prompt


class NativeNewChatWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'newchat.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.ws1_path = self.root / "alpha"
        self.ws2_path = self.root / "beta"
        self.ws1_path.mkdir()
        self.ws2_path.mkdir()
        async with self.sessions() as db:
            db.add(User(id="u1", role="user", settings={}, created_at=1))
            db.add_all(
                [
                    Workspace(
                        id="ws-alpha",
                        user_id="u1",
                        path=str(self.ws1_path),
                        name="Alpha",
                        slug="alpha",
                        workspace_type="project",
                        data={},
                        created_at=1,
                        updated_at=1,
                    ),
                    Workspace(
                        id="ws-beta",
                        user_id="u1",
                        path=str(self.ws2_path),
                        name="Beta",
                        slug="beta",
                        workspace_type="project",
                        data={},
                        created_at=1,
                        updated_at=1,
                    ),
                ]
            )
            await db.commit()

        self.patches = [
            patch("cptr.models.chats.get_db", new=self._db),
            patch("cptr.models.workspaces.get_db", new=self._db),
            patch("cptr.services.workspace_refs.get_db", new=self._db),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        self.temp.cleanup()

    async def _db(self):
        return self.sessions()

    @staticmethod
    def _request():
        app = SimpleNamespace(state=SimpleNamespace())
        return SimpleNamespace(app=app)

    async def test_new_chat_binds_stable_id_keeps_legacy_path_and_strips_privilege_params(self):
        body = SendMessageRequest(
            content="hello",
            model_id="model",
            workspace_id="ws-alpha",
            workspace=str(self.ws1_path),
            params={
                "temperature": 0.2,
                "admin_role": "ADMIN",
                "mode": "ADMIN",
                "workbench_session_id": "wbs_other",
                "local_root_unrestricted": True,
            },
        )
        with (
            patch("cptr.routers.chat._get_user", return_value="u1"),
            patch(
                "cptr.utils.model_targets.resolve_model_target",
                new=AsyncMock(return_value=object()),
            ),
            patch("cptr.utils.chat_task.start_task"),
            patch("cptr.routers.chat.Runtime.write_file", new=AsyncMock()),
            patch("cptr.utils.chat_export.export_chat_to_file", new=AsyncMock()),
        ):
            result = await send_message(self._request(), body)

        chat = await Chat.get_by_id(result["chat_id"])
        self.assertEqual(str(chat.workspace_id), "ws-alpha")
        self.assertEqual(chat.meta["workspace"], str(self.ws1_path))
        self.assertEqual(chat.meta["workspace_role"], "NORMAL")
        self.assertEqual(chat.meta["params"], {"temperature": 0.2})

    async def test_conflicting_stable_id_and_legacy_path_fail_closed(self):
        with self.assertRaises(HTTPException) as ctx:
            await _resolve_chat_workspace(
                user_id="u1",
                workspace_id="ws-alpha",
                workspace=str(self.ws2_path),
            )
        self.assertEqual(ctx.exception.status_code, 409)

    async def test_fork_preserves_workspace_identity_but_not_privilege_context(self):
        source = await Chat.create(
            user_id="u1",
            title="source",
            workspace_id="ws-alpha",
            meta={
                "workspace": str(self.ws1_path),
                "workspace_role": "ADMIN",
                "admin_role": "ADMIN",
                "mode": "ADMIN",
                "privilege": "ADMIN",
                "workbench_session_id": "wbs_source",
                "params": {"temperature": 0.1, "admin_role": "ADMIN"},
            },
            created_at=1,
        )
        from cptr.models import ChatMessage

        user_msg = await ChatMessage.create(
            chat_id=source.id,
            role="user",
            content="hello",
            created_at=1,
        )
        assistant = await ChatMessage.create(
            chat_id=source.id,
            role="assistant",
            content="done",
            parent_id=user_msg.id,
            model="model",
            done=True,
            created_at=2,
        )
        await Chat.update_current_message(source.id, assistant.id, 2)

        with (
            patch("cptr.routers.chat._get_user", return_value="u1"),
            patch(
                "cptr.routers.chat._chat_has_active_generation", new=AsyncMock(return_value=False)
            ),
            patch("cptr.utils.chat_export.export_chat_to_file", new=AsyncMock()),
        ):
            result = await fork_chat(self._request(), source.id)

        fork = await Chat.get_by_id(result["chat_id"])
        self.assertEqual(str(fork.workspace_id), "ws-alpha")
        self.assertEqual(fork.meta["workspace_role"], "NORMAL")
        self.assertNotIn("admin_role", fork.meta)
        self.assertNotIn("mode", fork.meta)
        self.assertNotIn("privilege", fork.meta)
        self.assertNotIn("workbench_session_id", fork.meta)
        self.assertEqual(fork.meta["params"], {"temperature": 0.1})


class FirstReasoningContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_workspace_context_is_injected_and_snapshot_recorded_as_normal(self):
        with tempfile.TemporaryDirectory() as temp:
            context_result = {
                "context": {
                    "rendered": "# Workspace Context Snapshot [snap-1]\n- Workspace ID: ws-1\n## Environment Evidence\n- target: local",
                    "snapshot_id": "snap-1",
                    "content_digest": "digest-1",
                    "created_at_ms": 123,
                }
            }
            fake_chat = SimpleNamespace(
                id="chat-1",
                user_id="u1",
                meta={"workspace_role": "ADMIN", "admin_role": "ADMIN"},
            )
            update_meta = AsyncMock()
            with (
                patch(
                    "cptr.services.workspace_actions.workspace_action_service.context_for_chat",
                    new=AsyncMock(return_value=context_result),
                ) as compile_context,
                patch(
                    "cptr.utils.prompt_templates.Runtime.read_file",
                    new=AsyncMock(return_value={"binary": True}),
                ),
                patch("cptr.utils.prompt_templates.Config.get", new=AsyncMock(return_value=None)),
                patch(
                    "cptr.utils.prompt_templates.identity_for_user_id",
                    new=AsyncMock(side_effect=RuntimeError("no identity")),
                ),
                patch("cptr.models.Chat.get_by_id", new=AsyncMock(return_value=fake_chat)),
                patch("cptr.models.Chat.update_meta", new=update_meta),
            ):
                prompt = await load_system_prompt(
                    MagicMock(),
                    workspace=temp,
                    model="model",
                    user_id="u1",
                    current_message="implement feature",
                    recent_messages=[{"role": "user", "content": "implement feature"}],
                    memory_task_key="msg-1",
                    workspace_id="ws-1",
                    chat_id="chat-1",
                )

            self.assertIn("Workspace Context Snapshot [snap-1]", prompt)
            compile_context.assert_awaited_once()
            saved_meta = update_meta.await_args.args[1]
            self.assertEqual(saved_meta["workspace_context"]["snapshot_id"], "snap-1")
            self.assertEqual(saved_meta["workspace_context"]["role"], "NORMAL")
            self.assertEqual(saved_meta["workspace_role"], "NORMAL")


if __name__ == "__main__":
    unittest.main()
