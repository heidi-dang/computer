"""Centralized redaction for task output, evidence, events, and logs.

This module deliberately treats redaction as a boundary concern.  Provider and
browser integrations commonly put credentials in URL query strings or plain
text headers, so key-only dictionary filtering is not sufficient.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any

REDACTED = "[REDACTED]"
MAX_STRING_LENGTH = 10_000

_SENSITIVE_KEYS = {
    "password",
    "hashedpassword",
    "token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "apikey",
    "secret",
    "authorization",
    "cookie",
    "setcookie",
    "session",
    "sessionid",
    "code",
    "state",
    "clientsecret",
    "key",
}
_SENSITIVE_SUFFIXES = (
    "token",
    "secret",
    "apikey",
    "password",
    "authorization",
    "cookie",
    "sessionid",
)

_QUERY_SECRET_RE = re.compile(
    r"([?&](?:access_token|refresh_token|id_token|api[_-]?key|token|secret|password|code|state|session(?:_id)?)[=])[^&#\s]+",
    re.IGNORECASE,
)
_HEADER_SECRET_RE = re.compile(
    r"(\b(?:authorization\s*:\s*(?:bearer|basic)|proxy-authorization\s*:\s*(?:bearer|basic)|cookie\s*:|set-cookie\s*:)[ \t]+)[^\r\n]+",
    re.IGNORECASE,
)
_INLINE_SECRET_RE = re.compile(
    r"(\b(?:access_token|refresh_token|id_token|api[_-]?key|token|secret|password|session[_-]?id)\s*[:=]\s*)[\"']?[^\s,;&\"'}]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{8,}|AIza[A-Za-z0-9_-]{20,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})\b"
)
_EXTERNAL_PATH_RE = re.compile(
    r"(?<![\w:])/(?:home|Users|tmp|var|opt|srv|private|workspace)(?:/[^\s\"'`<>]+)+"
    r"|(?<![\w:])[A-Za-z]:\\(?:[^\\\s\"'`<>]+\\)*[^\\\s\"'`<>]+"
)


def _key_is_sensitive(key: Any) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(suffix) for suffix in _SENSITIVE_SUFFIXES
    )


def redact_text(value: str) -> str:
    """Redact credentials in human text, URLs, headers, and JSON strings."""
    if not isinstance(value, str):
        return value

    text = value
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None and isinstance(parsed, (dict, list)):
        return json.dumps(redact_sensitive(parsed), ensure_ascii=False, separators=(",", ":"))

    text = _QUERY_SECRET_RE.sub(rf"\1{REDACTED}", text)
    text = _HEADER_SECRET_RE.sub(rf"\1{REDACTED}", text)
    text = _INLINE_SECRET_RE.sub(rf"\1{REDACTED}", text)
    text = _BEARER_RE.sub(f"Bearer {REDACTED}", text)
    text = _KNOWN_TOKEN_RE.sub(REDACTED, text)
    if len(text) > MAX_STRING_LENGTH:
        text = f"{text[:MAX_STRING_LENGTH]}..."
    return text


def redact_external_text(value: str) -> str:
    """Redact secrets and host filesystem paths at an external API boundary."""
    return _EXTERNAL_PATH_RE.sub("<workspace-path>", redact_text(value))


def redact_sensitive(value: Any) -> Any:
    """Return a recursively redacted, JSON-compatible copy of ``value``."""
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    elif is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)

    if isinstance(value, dict):
        return {
            key: REDACTED if _key_is_sensitive(key) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, set):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_external(value: Any) -> Any:
    """Recursively apply secret and host-path redaction to returned data."""
    if isinstance(value, dict):
        return {key: redact_external(item) for key, item in redact_sensitive(value).items()}
    if isinstance(value, list):
        return [redact_external(item) for item in redact_sensitive(value)]
    if isinstance(value, tuple):
        return [redact_external(item) for item in redact_sensitive(value)]
    if isinstance(value, str):
        return redact_external_text(value)
    return value


__all__ = ["REDACTED", "redact_external", "redact_external_text", "redact_sensitive", "redact_text"]
