"""Shared control-plane boundary over CPTR's existing agent lifecycle."""

from __future__ import annotations

import time
import uuid
import hashlib
from typing import Any

from cptr.env import TASK_CANCELLATION_TIMEOUT_SECONDS
from cptr.models import Chat, ChatMessage, ControlTask, Workspace
from cptr.services.control_store import ControlTaskStore
from cptr.utils.db import get_db


class AgentService:
    """Start and inspect worker tasks without creating a second execution engine."""

    def __init__(self, *, store: ControlTaskStore | None = None) -> None:
        self.store = store or ControlTaskStore()

    async def start_task(
        self,
        *,
        user_id: str,
        workspace_id: str,
        prompt: str,
        model_id: str,
        idempotency_key: str | None = None,
        request: Any | None = None,
    ) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be blank")
        if not model_id.strip():
            raise ValueError("model_id must not be blank")

        if idempotency_key:
            existing = await self.store.by_idempotency(user_id, idempotency_key)
            if existing:
                return await self.get_task(existing.id, user_id=user_id)

        async with await get_db() as db:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.user_id != user_id:
                raise KeyError("workspace not found")

        task_id = f"task_{uuid.uuid4().hex[:20]}"
        now = int(time.time() * 1000)
        chat = await Chat.create(
            user_id=user_id,
            title=prompt[:80] or "Control task",
            meta={
                "workspace": workspace.path,
                "control_task_id": task_id,
                "internal": True,
                "control_plane": True,
            },
            created_at=now,
        )
        user_message = await ChatMessage.create(
            chat_id=chat.id,
            role="user",
            content=prompt,
            created_at=now,
        )
        assistant_message = await ChatMessage.create(
            chat_id=chat.id,
            role="assistant",
            content="",
            parent_id=user_message.id,
            model=model_id,
            done=False,
            created_at=now,
        )
        await Chat.update_current_message(chat.id, assistant_message.id, now)

        control_task = ControlTask(
            id=task_id,
            user_id=user_id,
            workspace_id=workspace_id,
            chat_id=chat.id,
            message_id=assistant_message.id,
            status="RUNNING",
            prompt=prompt,
            model_id=model_id,
            idempotency_key=idempotency_key,
            created_at=now,
            updated_at=now,
        )
        await self.store.create(control_task)

        task_request = request
        if task_request is None:
            from cptr.utils.identity import internal_request_for_user

            task_request = await internal_request_for_user(None, user_id)
        from cptr.utils.chat_task import start_task
        from cptr.utils.model_targets import resolve_model_target

        try:
            app_state = getattr(getattr(task_request, "app", None), "state", None)
            target = await resolve_model_target(model_id, app_state)
            start_task(
                task_request,
                message_id=assistant_message.id,
                chat_id=chat.id,
                user_id=user_id,
                workspace=workspace.path,
                target=target,
            )
        except Exception:
            await ChatMessage.update(
                assistant_message.id,
                done=True,
                meta={"error": "worker failed to start"},
            )
            await self.store.update(
                task_id,
                status="FAILED",
                error="worker failed to start",
                updated_at=int(time.time() * 1000),
            )
            raise
        return await self.get_task(task_id, user_id=user_id)

    async def start_existing_task(
        self,
        *,
        request: Any,
        message_id: str,
        chat_id: str,
        user_id: str,
        workspace: str,
        target: Any,
        output_queue: Any | None = None,
    ) -> dict[str, str]:
        """Start an already-materialized CPTR chat through the shared boundary."""
        from cptr.utils.chat_task import start_task

        start_task(
            request,
            message_id=message_id,
            chat_id=chat_id,
            user_id=user_id,
            workspace=workspace,
            output_queue=output_queue,
            target=target,
        )
        return {"chat_id": chat_id, "message_id": message_id, "status": "RUNNING"}

    async def get_task(self, task_id: str, *, user_id: str) -> dict[str, Any]:
        task = await self.store.get(task_id)
        if task is None or task.user_id != user_id:
            raise KeyError("task not found")
        message = await ChatMessage.get_by_id(task.message_id)
        if message is None:
            raise KeyError("task output not found")
        status = task.status
        error = (message.meta or {}).get("error") if isinstance(message.meta, dict) else None
        if message.done:
            desired_status = (
                status
                if status in {"CANCEL_REQUESTED", "CANCELLED"}
                else ("FAILED" if error else "COMPLETE")
            )
            if desired_status != status:
                transition = getattr(self.store, "transition_terminal", None)
                if callable(transition) and task.__class__ is ControlTask:
                    won = await transition(
                        task.id,
                        status=desired_status,
                        error=error,
                        updated_at=int(time.time() * 1000),
                    )
                    if won:
                        status = desired_status
                    else:
                        current = await self.store.get(task.id)
                        status = current.status if current else status
                else:
                    status = desired_status
        else:
            from cptr.utils.chat_task import is_running

            if status in {"CANCELLED", "COMPLETE", "FAILED"}:
                # A durable terminal transition wins over a late worker
                # heartbeat or a message row that has not flushed yet.
                pass
            elif is_running(message.id):
                status = "RUNNING"
            elif status in {"RUNNING", "PENDING"}:
                status = "FAILED"
                restart_error = error or "interrupted by CPTR restart"
                await ChatMessage.update(
                    message.id,
                    done=True,
                    meta={"error": restart_error},
                )
                error = restart_error
                transition = getattr(self.store, "transition_terminal", None)
                if callable(transition) and task.__class__ is ControlTask:
                    await transition(
                        task.id,
                        status=status,
                        error="interrupted by CPTR restart",
                        updated_at=int(time.time() * 1000),
                    )
                else:
                    await self.store.update(
                        task.id,
                        status=status,
                        error="interrupted by CPTR restart",
                        updated_at=int(time.time() * 1000),
                    )
        output = message.content or ""
        if status != task.status or task.output != output:
            await self.store.update(
                task.id,
                status=status,
                output={"content": output},
                updated_at=int(time.time() * 1000),
            )
        return {
            "id": task.id,
            "workspace_id": task.workspace_id,
            "chat_id": task.chat_id,
            "message_id": task.message_id,
            "status": status,
            "prompt": task.prompt,
            "model_id": task.model_id,
            "output": output,
            "raw_output": message.output or [],
            "error": error,
            "created_at": task.created_at,
            "updated_at": task.updated_at,
        }

    async def get_output(self, task_id: str, *, user_id: str) -> dict[str, Any]:
        task = await self.get_task(task_id, user_id=user_id)
        return {
            "task_id": task["id"],
            "status": task["status"],
            "content": task["output"],
            "raw_output": task["raw_output"],
        }

    async def send_message(
        self,
        task_id: str,
        *,
        user_id: str,
        content: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        task = await self.store.get(task_id)
        if task is None or task.user_id != user_id:
            raise KeyError("task not found")
        content = content.strip()
        if not content:
            raise ValueError("message must not be blank")
        now = int(time.time() * 1000)
        dedupe_key = (
            idempotency_key or hashlib.sha256(f"{task.id}\0{content}".encode("utf-8")).hexdigest()
        )
        control_message = await self.store.enqueue_message(
            task_id=task.id,
            user_id=user_id,
            chat_id=task.chat_id,
            content=content,
            dedupe_key=dedupe_key,
            chat_message_id=None,
            now=now,
        )
        message_id = control_message.chat_message_id
        if not message_id:
            # Recover the bind if the process stopped after committing the
            # control row or chat row but before the second update.
            existing_messages = await ChatMessage.get_all_by_chat(task.chat_id)
            existing = next(
                (
                    item
                    for item in existing_messages
                    if (item.meta or {}).get("control_message_id") == control_message.id
                ),
                None,
            )
            if existing is not None:
                message_id = existing.id
                await self.store.update_message(control_message.id, chat_message_id=message_id)
        if not message_id:
            message = await ChatMessage.create(
                chat_id=task.chat_id,
                role="user",
                content=content,
                meta={
                    "queued": True,
                    "delivery_status": "QUEUED",
                    "control_task_id": task.id,
                    "control_message_id": control_message.id,
                },
                created_at=now,
            )
            message_id = message.id
            await self.store.update_message(control_message.id, chat_message_id=message_id)
        from cptr.utils.chat_task import process_pending_chat_inputs

        from cptr.utils.chat_task import get_active_chat_ids, is_running

        if not is_running(task.message_id) and task.chat_id not in get_active_chat_ids():
            from cptr.utils.identity import internal_request_for_user

            request = await internal_request_for_user(None, user_id)
            chat = await Chat.get_by_id(task.chat_id)
            workspace = (chat.meta or {}).get("workspace", "") if chat else ""
            await process_pending_chat_inputs(request, task.chat_id, user_id, workspace)
            refreshed = await self.store.get_message(control_message.id)
            if refreshed is not None:
                control_message = refreshed
        return {
            "task_id": task.id,
            "message_id": message_id,
            "control_message_id": control_message.id,
            "status": "QUEUED",
            "delivery_status": control_message.status,
        }

    async def cancel_task(self, task_id: str, *, user_id: str) -> dict[str, Any]:
        task = await self.store.get(task_id)
        if task is None or task.user_id != user_id:
            raise KeyError("task not found")
        now = int(time.time() * 1000)
        request_cancel = getattr(self.store, "request_cancel", None)
        transition = getattr(self.store, "transition_terminal", None)
        if callable(request_cancel):
            won = await request_cancel(task.id, requested_at=now)
            current = await self.store.get(task.id)
            if not won and current and current.status not in {"CANCEL_REQUESTED"}:
                result = await self.get_task(task.id, user_id=user_id)
                result["cancelled"] = False
                result["cancel_race"] = (
                    "completion_won" if current.status == "COMPLETE" else "terminal_state_won"
                )
                return result
        elif callable(transition):
            won = await transition(
                task.id,
                status="CANCELLED",
                cancelled_at=now,
                updated_at=now,
                error="cancelled",
            )
            if not won:
                current = await self.store.get(task.id)
                result = await self.get_task(task.id, user_id=user_id)
                result["cancelled"] = False
                result["cancel_race"] = (
                    "completion_won"
                    if current and current.status == "COMPLETE"
                    else "terminal_state_won"
                )
                return result
        invalidate = getattr(self.store, "invalidate_messages_for_task", None)
        if callable(invalidate):
            await invalidate(task.id, now=now)
        from cptr.utils.chat_task import cancel_task

        quiescent = await cancel_task(
            task.message_id,
            timeout=TASK_CANCELLATION_TIMEOUT_SECONDS,
        )
        if not quiescent:
            result = await self.get_task(task.id, user_id=user_id)
            result["cancelled"] = False
            result["cancellation_status"] = "BLOCKED"
            result["error"] = "owned execution did not quiesce within the cancellation bound"
            return result
        finalize = getattr(self.store, "finalize_cancel", None)
        if callable(finalize):
            await finalize(task.id, cancelled_at=now, updated_at=int(time.time() * 1000))
        elif not callable(transition):
            await ChatMessage.update(
                task.message_id,
                done=True,
                meta={"error": "cancelled"},
            )
        result = await self.get_task(task.id, user_id=user_id)
        result["cancelled"] = True
        return result

    async def get_diff(self, workspace_id: str, *, user_id: str) -> dict[str, Any]:
        async with await get_db() as db:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.user_id != user_id:
                raise KeyError("workspace not found")
        from cptr.utils.git import diff, is_repo
        from cptr.utils.identity import identity_for_user_id

        identity = await identity_for_user_id(user_id)
        if not await is_repo(workspace.path, identity):
            return {"is_repo": False, "files": [], "diagnostic": "not a git repository"}
        result = await diff(workspace.path, None, False, True, False, identity)
        result["is_repo"] = True
        return result

    async def get_verification_evidence(self, workspace_id: str, *, user_id: str) -> dict[str, Any]:
        async with await get_db() as db:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.user_id != user_id:
                raise KeyError("workspace not found")
        from cptr.utils.git import diff_check, status
        from cptr.utils.identity import identity_for_user_id

        identity = await identity_for_user_id(user_id)
        return {
            "workspace_path": workspace.path,
            "git_status": await status(workspace.path, identity),
            "git_diff_check": await diff_check(workspace.path, identity),
        }
