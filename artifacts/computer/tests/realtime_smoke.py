#!/usr/bin/env python3
"""Authenticated Socket.IO polling smoke check for a running CPTR server.

This intentionally uses HTTP polling instead of python-socketio's optional
aiohttp/requests transports.  ``httpx`` is a normal CPTR dependency, so the
runner works after a plain ``pip install cptr``.

Examples:
    python tests/realtime_smoke.py --url http://127.0.0.1:8000 \
      --cookie "$CPTR_SESSION_COOKIE" --message "Say hello" --model-id openai/gpt-4o
    python tests/realtime_smoke.py --url https://example.test \
      --token "$CPTR_SESSION_TOKEN" --chat-id CHAT_ID
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx


def split_engine_packets(body: str) -> list[str]:
    """Decode Engine.IO polling's record separator framing."""
    return [packet for packet in body.split("\x1e") if packet]


def decode_socket_event(packet: str) -> tuple[str, Any] | None:
    """Return a Socket.IO event name and payload from an Engine.IO packet."""
    if not packet.startswith("42"):
        return None
    try:
        event, payload = json.loads(packet[2:])
    except (ValueError, TypeError):
        return None
    return event, payload


def classify_assistant_messages(messages: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Separate useful assistant output from provider-error output."""
    result = {"success_text": [], "provider_error_text": []}
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        meta = message.get("meta") or {}
        if isinstance(meta, dict) and meta.get("error"):
            result["provider_error_text"].append(content)
        else:
            result["success_text"].append(content)
    return result


class PollingSocket:
    """Minimal Engine.IO v4 / Socket.IO v4 polling client."""

    def __init__(self, client: httpx.Client, socket_url: str, token: str | None):
        self.client = client
        self.socket_url = socket_url
        self.token = token
        self.sid: str | None = None

    def connect(self) -> None:
        response = self.client.get(
            self.socket_url,
            params={"EIO": "4", "transport": "polling"},
        )
        response.raise_for_status()
        packets = split_engine_packets(response.text)
        if not packets or not packets[0].startswith("0"):
            raise RuntimeError(f"unexpected Engine.IO handshake: {response.text[:200]}")
        handshake = json.loads(packets[0][1:])
        self.sid = handshake["sid"]
        auth = json.dumps({"token": self.token}, separators=(",", ":")) if self.token else ""
        self._post("40" + auth)
        self._poll()  # consume Socket.IO's connect acknowledgement

    def _post(self, body: str) -> None:
        response = self.client.post(
            self.socket_url,
            params={"EIO": "4", "transport": "polling", "sid": self.sid},
            content=body,
            headers={"Content-Type": "text/plain; charset=UTF-8"},
        )
        response.raise_for_status()

    def _poll(self) -> list[str]:
        response = self.client.get(
            self.socket_url,
            params={"EIO": "4", "transport": "polling", "sid": self.sid},
        )
        response.raise_for_status()
        packets = split_engine_packets(response.text)
        if "2" in packets:  # Engine.IO ping; answer before the next poll.
            self._post("3")
        return packets

    def events_until(self, deadline: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            for packet in self._poll():
                decoded = decode_socket_event(packet)
                if decoded:
                    name, payload = decoded
                    events.append({"event": name, "data": payload})
                    if (
                        name == "events:chat"
                        and isinstance(payload, dict)
                        and (payload.get("done") or payload.get("error"))
                    ):
                        return events
        return events

    def close(self) -> None:
        if self.sid:
            try:
                self._post("41")
            except httpx.HTTPError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="CPTR base URL, without /socket.io")
    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument("--cookie", help="cptr_session value or complete cookie pair")
    auth.add_argument("--token", help="CPTR bearer/session token")
    parser.add_argument("--chat-id", help="Existing chat to observe")
    parser.add_argument("--message", help="Send a prompt before observing")
    parser.add_argument("--model-id", help="Model ID required with --message")
    parser.add_argument("--workspace", help="Workspace path for a new chat")
    parser.add_argument("--timeout", type=float, default=60, help="Seconds to wait for events")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.message) != bool(args.model_id):
        raise SystemExit("--message and --model-id must be provided together")
    base_url = args.url.rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--url must be an absolute http:// or https:// URL")
    cookies = {}
    token = args.token
    if args.cookie:
        cookie = args.cookie.strip()
        cookies["cptr_session"] = cookie.split("=", 1)[1] if "=" in cookie else cookie

    with httpx.Client(base_url=base_url, cookies=cookies, timeout=30) as client:
        socket = PollingSocket(client, "/socket.io/", token)
        try:
            socket.connect()
            chat_id = args.chat_id
            requested_message_id = None
            if args.message:
                payload = {
                    "content": args.message,
                    "model_id": args.model_id,
                    **({"chat_id": chat_id} if chat_id else {}),
                    **({"workspace": args.workspace} if args.workspace else {}),
                }
                response = client.post("/api/chats", json=payload)
                response.raise_for_status()
                sent = response.json()
                chat_id = sent["chat_id"]
                requested_message_id = sent["message_id"]

            if not chat_id:
                raise SystemExit("--chat-id is required when no message is sent")

            events = socket.events_until(time.monotonic() + args.timeout)
            response = client.get(f"/api/chats/{chat_id}")
            response.raise_for_status()
            messages = response.json().get("messages", [])
            correlated = [
                event
                for event in events
                if event["event"] == "events:chat"
                and isinstance(event.get("data"), dict)
                and event["data"].get("message_id") in {message.get("id") for message in messages}
            ]
            report = {
                "chat_id": chat_id,
                "requested_message_id": requested_message_id,
                "event_count": len(events),
                "events_chat_count": sum(event["event"] == "events:chat" for event in events),
                "correlated_event_count": len(correlated),
                "messages": classify_assistant_messages(messages),
            }
            print(json.dumps(report, indent=2))
            if not events or not correlated:
                return 1
            return 0
        finally:
            socket.close()


if __name__ == "__main__":
    sys.exit(main())
