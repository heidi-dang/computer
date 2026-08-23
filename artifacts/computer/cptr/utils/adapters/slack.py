"""Slack adapter — zero SDK dependencies.

Uses Socket Mode WebSocket for receiving events (no public URL needed)
and the Slack Web API for sending messages via httpx.

Token format: ``xoxb-bottoken|xapp-apptoken`` (pipe-separated).
The bot token is used for API calls, the app-level token for Socket Mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import httpx

from cptr.utils.bridge import Attachment, BaseAdapter, MessageEvent, chunk_message

logger = logging.getLogger(__name__)

API_BASE = "https://slack.com/api"
MAX_MESSAGE_LEN = 4000  # Slack max is ~4000 for text blocks
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0


def _parse_token(token: str) -> tuple[str, str]:
    """Parse 'bot_token|app_token' format."""
    if "|" in token:
        bot, app = token.split("|", 1)
        return bot.strip(), app.strip()
    raise ValueError("Slack token must be 'xoxb-...|xapp-...' (pipe-separated)")


class SlackAdapter(BaseAdapter):
    """Slack bot via Socket Mode WebSocket + Web API."""

    platform = "slack"

    def __init__(self, token: str) -> None:
        super().__init__()
        bot_token, app_token = _parse_token(token)
        self._bot_token = bot_token
        self._app_token = app_token
        self._http: Optional[httpx.AsyncClient] = None
        self._ws = None
        self._running = False
        self._bot_id: str = ""
        self._gateway_task: Optional[asyncio.Task] = None

    # ── Lifecycle ──────────────────────────────────────────

    async def connect(self) -> bool:
        self._http = httpx.AsyncClient(
            timeout=15,
            headers={"Authorization": f"Bearer {self._bot_token}"},
        )

        try:
            resp = await self._http.post(f"{API_BASE}/auth.test")
            data = resp.json()
            if not data.get("ok"):
                raise ValueError(data.get("error", "auth.test failed"))
            self._bot_id = data.get("user_id", "")
            logger.info("Slack bot connected: %s (team: %s)", data.get("user", "?"), data.get("team", "?"))
        except Exception:
            logger.exception("Slack auth.test failed")
            await self._http.aclose()
            self._http = None
            return False

        self._running = True
        self._gateway_task = asyncio.create_task(self._socket_mode_loop())
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._gateway_task and not self._gateway_task.done():
            self._gateway_task.cancel()
            try:
                await self._gateway_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._http:
            await self._http.aclose()
            self._http = None

    # ── Sending ────────────────────────────────────────────

    async def send(self, chat_id: str, text: str) -> str | None:
        if not self._http:
            return None
        chunks = chunk_message(text, MAX_MESSAGE_LEN)
        msg_ts = None
        for chunk in chunks:
            resp = await self._http.post(
                f"{API_BASE}/chat.postMessage",
                json={"channel": chat_id, "text": chunk},
            )
            data = resp.json()
            if msg_ts is None and data.get("ok"):
                msg_ts = data.get("ts")
        return msg_ts

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        if not self._http:
            return
        await self._http.post(
            f"{API_BASE}/chat.update",
            json={
                "channel": chat_id,
                "ts": message_id,
                "text": text[:MAX_MESSAGE_LEN],
            },
        )

    async def send_typing(self, chat_id: str) -> None:
        # Slack doesn't have a direct typing indicator API for bots
        pass

    # ── Socket Mode ────────────────────────────────────────

    async def _socket_mode_loop(self) -> None:
        delay = RECONNECT_BASE_DELAY
        while self._running:
            try:
                await self._run_socket_session()
                delay = RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Slack Socket Mode error (%s), reconnecting in %.0fs", type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _run_socket_session(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("Slack adapter requires 'websockets' package. Install: pip install websockets")
            self._running = False
            return

        # Get WebSocket URL via apps.connections.open
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{API_BASE}/apps.connections.open",
                headers={"Authorization": f"Bearer {self._app_token}"},
            )
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"apps.connections.open failed: {data.get('error')}")
            ws_url = data["url"]

        async with websockets.connect(ws_url) as ws:
            self._ws = ws

            async for raw in ws:
                if not self._running:
                    return

                payload = json.loads(raw)
                msg_type = payload.get("type")

                # Acknowledge all envelopes
                envelope_id = payload.get("envelope_id")
                if envelope_id:
                    await ws.send(json.dumps({"envelope_id": envelope_id}))

                if msg_type == "disconnect":
                    logger.info("Slack Socket Mode disconnect requested")
                    return

                if msg_type == "events_api":
                    event = payload.get("payload", {}).get("event", {})
                    await self._handle_event(event)

    async def _handle_event(self, event: dict) -> None:
        if event.get("type") != "message":
            return
        # Ignore bot messages, subtypes (edits, joins, etc.)
        if event.get("bot_id") or event.get("subtype"):
            return
        if event.get("user") == self._bot_id:
            return

        text = (event.get("text") or "").strip()

        # Process file attachments
        attachments: list[Attachment] = []
        for f in event.get("files", []):
            url = f.get("url_private")
            if not url:
                continue
            file_data = await self._download_file(url)
            if not file_data:
                continue
            fname = f.get("name") or f.get("title") or "file"
            mime = f.get("mimetype", "application/octet-stream")
            if mime.startswith("image/"):
                att_type = "image"
            elif mime.startswith("audio/"):
                att_type = "audio"
            else:
                att_type = "document"
            attachments.append(Attachment(
                type=att_type, filename=fname, data=file_data, mime_type=mime,
            ))

        if not text and not attachments:
            return

        # Resolve display name
        user_id = event.get("user", "")
        sender_name = await self._resolve_name(user_id)

        msg_event = MessageEvent(
            platform="slack",
            chat_id=event.get("channel", ""),
            sender_id=user_id,
            sender_name=sender_name,
            text=text,
            attachments=attachments,
        )

        if self.on_message:
            await self.on_message(msg_event)

    async def _resolve_name(self, user_id: str) -> str:
        if not self._http or not user_id:
            return "User"
        try:
            resp = await self._http.get(f"{API_BASE}/users.info", params={"user": user_id})
            data = resp.json()
            if data.get("ok"):
                user = data.get("user", {})
                return user.get("real_name") or user.get("name") or "User"
        except Exception:
            pass
        return "User"

    async def _download_file(self, url: str) -> bytes | None:
        """Download a file from Slack (requires bot token auth)."""
        if not self._http:
            return None
        try:
            resp = await self._http.get(url)
            if resp.status_code == 200:
                return resp.content
            logger.warning("[slack] File download failed: HTTP %d", resp.status_code)
        except Exception:
            logger.exception("[slack] Failed to download %s", url)
        return None

    async def send_photo(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> str | None:
        """Send a photo via Slack file upload."""
        return await self._upload_file(chat_id, data, filename, caption)

    async def send_document(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> str | None:
        """Send a document via Slack file upload."""
        return await self._upload_file(chat_id, data, filename, caption)

    async def _upload_file(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> str | None:
        """Upload a file to Slack via files.uploadV2."""
        if not self._http:
            return None
        try:
            # Step 1: Get upload URL
            resp = await self._http.get(
                f"{API_BASE}/files.getUploadURLExternal",
                params={"filename": filename, "length": len(data)},
            )
            url_data = resp.json()
            if not url_data.get("ok"):
                logger.warning("[slack] files.getUploadURLExternal failed: %s", url_data.get("error"))
                return None

            # Step 2: Upload to the provided URL
            upload_url = url_data["upload_url"]
            file_id = url_data["file_id"]
            await self._http.post(upload_url, content=data)

            # Step 3: Complete the upload
            await self._http.post(
                f"{API_BASE}/files.completeUploadExternal",
                json={
                    "files": [{"id": file_id, "title": filename}],
                    "channel_id": chat_id,
                    "initial_comment": caption or "",
                },
            )
            return file_id
        except Exception:
            logger.exception("[slack] Failed to upload file")
        return None



async def verify_token(token: str) -> dict:
    """Verify a Slack bot token by calling auth.test."""
    bot_token, app_token = _parse_token(token)

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{API_BASE}/auth.test",
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        data = resp.json()
        if not data.get("ok"):
            raise ValueError(data.get("error", "Invalid Slack token"))

        # Also verify app token
        resp2 = await client.post(
            f"{API_BASE}/apps.connections.open",
            headers={"Authorization": f"Bearer {app_token}"},
        )
        data2 = resp2.json()
        if not data2.get("ok"):
            raise ValueError(f"App token error: {data2.get('error', 'Invalid app token')}")

        return {"username": data.get("user"), "id": data.get("user_id"), "team": data.get("team")}
