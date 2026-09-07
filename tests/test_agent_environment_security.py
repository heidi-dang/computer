import os
from pathlib import Path
from unittest.mock import patch

import cptr
from cptr.utils.agents.environment import agent_env, github_cli_env
from cptr.utils.identity import ExecutionIdentity, env_for


def _identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        app_user_id="user-1",
        username="runner",
        uid=None,
        gid=None,
        groups=(),
        home="/home/runner",
        shell="/bin/sh",
        is_pam=False,
    )


def test_agent_environment_grants_only_the_selected_provider_credentials() -> None:
    with patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin:/bin",
            "OPENAI_API_KEY": "openai-token",
            "ANTHROPIC_API_KEY": "anthropic-token",
            "XAI_API_KEY": "xai-token",
            "CPTR_BEARER_TOKEN": "must-never-reach-child",
        },
        clear=True,
    ):
        codex = agent_env({"agent": "codex"}, _identity(), "/workspace")
        grok = agent_env({"agent": "grok"}, _identity(), "/workspace")

    assert codex["OPENAI_API_KEY"] == "openai-token"
    assert "ANTHROPIC_API_KEY" not in codex
    assert "XAI_API_KEY" not in codex
    assert "CPTR_BEARER_TOKEN" not in codex

    assert grok["XAI_API_KEY"] == "xai-token"
    assert "OPENAI_API_KEY" not in grok
    assert "ANTHROPIC_API_KEY" not in grok
    assert "CPTR_BEARER_TOKEN" not in grok


def test_multi_provider_agent_receives_bounded_api_keys_not_service_secrets() -> None:
    with patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin:/bin",
            "OPENAI_API_KEY": "openai-token",
            "ANTHROPIC_API_KEY": "anthropic-token",
            "AWS_SECRET_ACCESS_KEY": "infrastructure-secret",
            "CPTR_LIVE_TICKET_SECRET": "service-secret",
        },
        clear=True,
    ):
        child = agent_env({"agent": "opencode"}, _identity(), "/workspace")

    assert child["OPENAI_API_KEY"] == "openai-token"
    assert child["ANTHROPIC_API_KEY"] == "anthropic-token"
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert "CPTR_LIVE_TICKET_SECRET" not in child


def test_internal_probe_without_authenticated_identity_still_uses_sanitized_environment() -> None:
    with patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/service",
            "USER": "service",
            "SHELL": "/bin/bash",
            "CPTR_SENTINEL_SECRET": "must-not-leak",
        },
        clear=True,
    ):
        child = env_for(None, "/probe")

    assert child["HOME"] == "/home/service"
    assert child["USER"] == "service"
    assert child["PWD"] == "/probe"
    assert "CPTR_SENTINEL_SECRET" not in child


def test_github_cli_environment_exposes_only_explicit_token_auth() -> None:
    with patch.dict(
        os.environ,
        {
            "PATH": "/usr/bin:/bin",
            "GH_TOKEN": "gh-token",
            "GITHUB_TOKEN": "github-token",
            "CPTR_BEARER_TOKEN": "service-secret",
        },
        clear=True,
    ):
        command_env = github_cli_env(
            _identity(),
            "/workspace",
            include_token_auth=True,
            extra={"GH_PROMPT_DISABLED": "1"},
        )
        login_env = github_cli_env(_identity(), "/workspace", include_token_auth=False)

    assert command_env["GH_TOKEN"] == "gh-token"
    assert command_env["GITHUB_TOKEN"] == "github-token"
    assert command_env["GH_PROMPT_DISABLED"] == "1"
    assert "CPTR_BEARER_TOKEN" not in command_env
    assert "GH_TOKEN" not in login_env
    assert "GITHUB_TOKEN" not in login_env
    assert "CPTR_BEARER_TOKEN" not in login_env


def test_execution_adapters_never_copy_the_service_environment_wholesale() -> None:
    package_root = Path(cptr.__file__).resolve().parent
    sources = [package_root / "utils" / "gh.py", *(package_root / "utils" / "agents").glob("*.py")]
    offenders = [
        path.relative_to(package_root).as_posix()
        for path in sources
        if "os.environ.copy()" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
