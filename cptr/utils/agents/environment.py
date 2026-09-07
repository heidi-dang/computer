"""Minimal, provider-scoped environments for coding-agent subprocesses.

Agent processes must not inherit the CPTR service environment wholesale.  They
receive the same sanitized execution baseline as ordinary commands plus only
provider variables explicitly approved for that agent family.  Cached CLI
OAuth remains available through the execution identity's HOME directory.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from cptr.utils.identity import ExecutionIdentity, env_for

_MULTI_PROVIDER_API_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "COHERE_API_KEY",
        "DEEPSEEK_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GROQ_API_KEY",
        "MISTRAL_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "XAI_API_KEY",
    }
)

_AGENT_ENV_KEYS: dict[str, frozenset[str]] = {
    "codex": frozenset(
        {
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "OPENAI_ORG_ID",
            "OPENAI_PROJECT_ID",
        }
    ),
    "claude_code": frozenset(
        {
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN",
        }
    ),
    "cursor": frozenset({"CURSOR_API_KEY", "CURSOR_AGENT_API_KEY"}),
    "grok": frozenset({"XAI_API_KEY"}),
    "gemini": frozenset(
        {
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GOOGLE_CLOUD_LOCATION",
            "GOOGLE_CLOUD_PROJECT",
            "GOOGLE_GENAI_USE_VERTEXAI",
        }
    ),
    # These clients multiplex multiple providers.  Keep the grant to bounded
    # provider API-key variables; infrastructure credentials remain excluded.
    "opencode": _MULTI_PROVIDER_API_KEYS,
    "cline": _MULTI_PROVIDER_API_KEYS,
    "pi": _MULTI_PROVIDER_API_KEYS,
}


def _selected_parent_env(keys: Iterable[str]) -> dict[str, str]:
    return {
        key: value
        for key in keys
        if (value := os.environ.get(key)) is not None and value != ""
    }


def agent_env(
    profile: dict[str, Any],
    identity: ExecutionIdentity | None,
    cwd: str,
    *,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a sanitized child environment with a bounded provider grant."""

    agent = str(profile.get("agent") or "").strip()
    granted = _selected_parent_env(_AGENT_ENV_KEYS.get(agent, frozenset()))
    if extra:
        granted.update({str(key): str(value) for key, value in extra.items()})
    return env_for(identity, cwd, granted)


def github_cli_env(
    identity: ExecutionIdentity | None,
    cwd: str,
    *,
    include_token_auth: bool,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a sanitized GitHub CLI environment.

    Normal commands may use explicitly named GitHub token variables.  Interactive
    login intentionally excludes them so `gh auth login` operates on the user's
    HOME-backed credential store rather than an ambient service token.
    """

    granted: dict[str, str] = {}
    if include_token_auth:
        granted.update(
            _selected_parent_env(
                {
                    "GH_ENTERPRISE_TOKEN",
                    "GH_TOKEN",
                    "GITHUB_ENTERPRISE_TOKEN",
                    "GITHUB_TOKEN",
                }
            )
        )
    if extra:
        granted.update({str(key): str(value) for key, value in extra.items()})
    return env_for(identity, cwd, granted)
