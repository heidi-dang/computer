"""Resolve provider credentials from encrypted connections or Replit AI env vars."""

from __future__ import annotations

import os

from cptr.utils.config import _get_jwt_secret
from cptr.utils.crypto import decrypt_key


def connection_api_key(connection: dict) -> str:
    """Return the runtime key without exposing credentials to callers."""
    if connection.get("managed_env") == "replit-openai":
        return os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY", "")
    stored = connection.get("api_key")
    return decrypt_key(stored, _get_jwt_secret()) if stored else ""


def managed_openai_connection() -> dict | None:
    """Build the ephemeral CPTR connection backed by Replit-managed OpenAI."""
    base_url = os.environ.get("AI_INTEGRATIONS_OPENAI_BASE_URL")
    api_key = os.environ.get("AI_INTEGRATIONS_OPENAI_API_KEY")
    if not base_url or not api_key:
        return None
    return {
        "id": "replit-managed-openai",
        "provider": "openai",
        "display_name": "ChatGPT",
        "base_url": base_url.rstrip("/"),
        "api_type": "chat_completions",
        "provider_type": "default",
        "prefix_id": "chatgpt",
        "managed_env": "replit-openai",
        "enabled": True,
        "data": {"models": ["gpt-5.6-terra"]},
    }