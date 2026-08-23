"""Discord adapter — zero SDK dependencies.

Uses raw WebSocket for the Discord Gateway and httpx for the REST API.
- Gateway v10 WebSocket: IDENTIFY → HEARTBEAT loop → MESSAGE_CREATE
- REST: ``POST /channels/{id}/messages`` for sending
- REST: ``PATCH /channels/{id}/messages/{id}`` for editing (streaming)
- REST: ``POST /channels/{id}/typing`` for typing indicators
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Optional

import httpx

from cptr.utils.bridge import Attachment, BaseAdapter, MessageEvent, chunk_message

logger = logging.getLogger(__name__)

GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
API_BASE = "https://discord.com/api/v10"
MAX_MESSAGE_LEN = 2000
RECONNECT_BASE_DELAY = 2.0
RECONNECT_MAX_DELAY = 60.0

# Gateway opcodes
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Required gateway intents
INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)


class DiscordAdapter(BaseAdapter):
    """Discord bot via raw Gateway WebSocket + REST API."""

    platform = "discord"

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token
        self._http: Optional[httpx.AsyncClient] = None
        self._ws = None
        self._running = False
        self._bot_id: str = ""
        self._session_id: str = ""
        self._resume_url: str = ""
        self._seq: Optional[int] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._gateway_task: Optional[asyncio.Task] = None

    # ── Lifecycle ──────────────────────────────────────────

    async def connect(self) -> bool:
        self._http = httpx.AsyncClient(
            timeout=15,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
            },
        )

        try:
            resp = await self._http.get(f"{API_BASE}/users/@me")
            resp.raise_for_status()
            me = resp.json()
            self._bot_id = me["id"]
            logger.info("Discord bot connected: %s#%s", me.get("username"), me.get("discriminator"))
        except Exception:
            logger.exception("Discord token verification failed")
            await self._http.aclose()
            self._http = None
            return False

        self._running = True
        self._gateway_task = asyncio.create_task(self._gateway_loop())
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
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
        """Send a message. Returns the message ID for later editing."""
        if not self._http:
            return None
        chunks = chunk_message(text, MAX_MESSAGE_LEN)
        msg_id = None
        for chunk in chunks:
            resp = await self._http.post(
                f"{API_BASE}/channels/{chat_id}/messages",
                json={"content": chunk},
            )
            if msg_id is None and resp.status_code == 200:
                data = resp.json()
                msg_id = data.get("id")
        return msg_id

    async def edit(self, chat_id: str, message_id: str, text: str) -> None:
        """Edit a previously sent message."""
        if not self._http:
            return
        await self._http.patch(
            f"{API_BASE}/channels/{chat_id}/messages/{message_id}",
            json={"content": text[:MAX_MESSAGE_LEN]},
        )

    async def send_typing(self, chat_id: str) -> None:
        if not self._http:
            return
        try:
            await self._http.post(f"{API_BASE}/channels/{chat_id}/typing")
        except Exception:
            pass

    # ── Gateway WebSocket ──────────────────────────────────

    async def _gateway_loop(self) -> None:
        delay = RECONNECT_BASE_DELAY
        while self._running:
            try:
                await self._run_gateway_session()
                delay = RECONNECT_BASE_DELAY
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Discord gateway error (%s), reconnecting in %.0fs", type(e).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)

    async def _run_gateway_session(self) -> None:
        try:
            import websockets
        except ImportError:
            logger.error("Discord adapter requires 'websockets' package. Install: pip install websockets")
            self._running = False
            return

        url = self._resume_url or GATEWAY_URL
        async with websockets.connect(url) as ws:
            self._ws = ws

            hello = json.loads(await ws.recv())
            if hello.get("op") != OP_HELLO:
                raise RuntimeError(f"Expected HELLO, got op={hello.get('op')}")

            heartbeat_interval = hello["d"]["heartbeat_interval"] / 1000.0
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(ws, heartbeat_interval))

            if self._session_id and self._seq is not None:
                await ws.send(json.dumps({
                    "op": OP_RESUME,
                    "d": {"token": self._token, "session_id": self._session_id, "seq": self._seq},
                }))
            else:
                await ws.send(json.dumps({
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": self._token,
                        "intents": INTENTS,
                        "properties": {"os": sys.platform, "browser": "cptr", "device": "cptr"},
                    },
                }))

            async for raw in ws:
                if not self._running:
                    return
                payload = json.loads(raw)
                op = payload.get("op")

                if op == OP_DISPATCH:
                    self._seq = payload.get("s")
                    event_type = payload.get("t")
                    data = payload.get("d", {})

                    if event_type == "READY":
                        self._session_id = data.get("session_id", "")
                        self._resume_url = data.get("resume_gateway_url", "")
                        logger.info("Discord gateway READY")
                    elif event_type == "MESSAGE_CREATE":
                        await self._handle_message_create(data)

                elif op == OP_HEARTBEAT:
                    await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._seq}))
                elif op == OP_RECONNECT:
                    return
                elif op == OP_INVALID_SESSION:
                    if not payload.get("d", False):
                        self._session_id = ""
                        self._seq = None
                    await asyncio.sleep(3)
                    return

    async def _heartbeat_loop(self, ws, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                await ws.send(json.dumps({"op": OP_HEARTBEAT, "d": self._seq}))
        except (asyncio.CancelledError, Exception):
            pass

    async def _handle_message_create(self, data: dict) -> None:
        author = data.get("author", {})
        if author.get("bot", False) or author.get("id") == self._bot_id:
            return

        content = data.get("content", "").strip()

        # Process Discord attachments
        attachments: list[Attachment] = []
        for att in data.get("attachments", []):
            url = att.get("url")
            if not url:
                continue
            file_data = await self._download_url(url)
            if not file_data:
                continue
            fname = att.get("filename", "file")
            ctype = att.get("content_type", "application/octet-stream")
            if ctype.startswith("image/"):
                att_type = "image"
            elif ctype.startswith("audio/"):
                att_type = "audio"
            else:
                att_type = "document"
            attachments.append(Attachment(
                type=att_type, filename=fname, data=file_data, mime_type=ctype,
            ))

        if not content and not attachments:
            return

        event = MessageEvent(
            platform="discord",
            chat_id=data.get("channel_id", ""),
            sender_id=author.get("id", ""),
            sender_name=author.get("global_name") or author.get("username", "User"),
            text=content,
            attachments=attachments,
        )

        if self.on_message:
            await self.on_message(event)

    async def _download_url(self, url: str) -> bytes | None:
        """Download a file from a URL (Discord CDN)."""
        if not self._http:
            return None
        try:
            resp = await self._http.get(url)
            if resp.status_code == 200:
                return resp.content
            logger.warning("[discord] File download failed: HTTP %d", resp.status_code)
        except Exception:
            logger.exception("[discord] Failed to download %s", url)
        return None

    async def send_photo(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> str | None:
        """Send a photo as a file attachment."""
        return await self._send_file(chat_id, data, filename, caption)

    async def send_document(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> str | None:
        """Send a document as a file attachment."""
        return await self._send_file(chat_id, data, filename, caption)

    async def _send_file(self, chat_id: str, data: bytes, filename: str, caption: str = "") -> str | None:
        """Send a file via Discord multipart upload."""
        if not self._http:
            return None
        try:
            files = {"files[0]": (filename, data, "application/octet-stream")}
            form_data = {}
            if caption:
                form_data["content"] = caption[:MAX_MESSAGE_LEN]
            resp = await self._http.post(
                f"{API_BASE}/channels/{chat_id}/messages",
                files=files,
                data=form_data,
            )
            if resp.status_code == 200:
                return resp.json().get("id")
        except Exception:
            logger.exception("[discord] Failed to send file")
        return None


async def verify_token(token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{API_BASE}/users/@me",
            headers={"Authorization": f"Bot {token}"},
        )
        if resp.status_code == 401:
            raise ValueError("Invalid Discord bot token")
        resp.raise_for_status()
        return resp.json()
