import os
from unittest.mock import patch

from click.testing import CliRunner
from fastapi import HTTPException

from cptr.cli import cli, should_open_dashboard, should_reveal_startup_token, startup_dashboard_urls
from cptr.routers.browser import resolve_create_session_mode


def test_dashboard_opening_requires_explicit_opt_in() -> None:
    assert not should_open_dashboard(open_browser=False, headless=False)
    assert not should_open_dashboard(open_browser=True, headless=True)
    assert should_open_dashboard(open_browser=True, headless=False)


def test_startup_dashboard_urls_separate_safe_display_from_secret_bootstrap() -> None:
    secret = "startup-secret-value"
    dashboard_url, bootstrap_url = startup_dashboard_urls(
        host="0.0.0.0", port=8000, token=secret
    )

    assert dashboard_url == "http://localhost:8000/"
    assert secret not in dashboard_url
    assert bootstrap_url == f"{dashboard_url}?token={secret}"


def test_startup_token_is_revealed_only_to_an_interactive_tty() -> None:
    class Stream:
        def __init__(self, interactive: bool) -> None:
            self.interactive = interactive

        def isatty(self) -> bool:
            return self.interactive

    assert should_reveal_startup_token(Stream(True))
    assert not should_reveal_startup_token(Stream(False))
    assert not should_reveal_startup_token(object())


def test_noninteractive_cli_output_hides_token_but_browser_keeps_bootstrap_url() -> None:
    secret = "startup-secret-value"
    with (
        patch.dict(os.environ, {}, clear=False),
        patch("secrets.token_hex", return_value=secret),
        patch("cptr.cli.should_reveal_startup_token", return_value=False),
        patch("cptr.cli.uvicorn.run") as uvicorn_run,
        patch("threading.Timer") as timer,
        patch("webbrowser.open") as browser_open,
    ):
        result = CliRunner().invoke(cli, ["run", "--host", "0.0.0.0", "--open-browser"])
        callback = timer.call_args.args[1]
        callback()

    assert result.exit_code == 0
    assert secret not in result.output
    assert "http://localhost:8000/" in result.output
    browser_open.assert_called_once_with(f"http://localhost:8000/?token={secret}")
    uvicorn_run.assert_called_once()


def test_personal_chrome_default_falls_back_to_proxy() -> None:
    assert (
        resolve_create_session_mode(
            {}, configured_default="chrome", personal_chrome_configured=True
        )
        == "proxy"
    )


def test_managed_chrome_default_remains_supported() -> None:
    assert (
        resolve_create_session_mode(
            {}, configured_default="chrome", personal_chrome_configured=False
        )
        == "chrome"
    )


def test_personal_chrome_requires_an_explicit_request() -> None:
    assert (
        resolve_create_session_mode(
            {"mode": "chrome"}, configured_default="proxy", personal_chrome_configured=True
        )
        == "chrome"
    )


def test_invalid_explicit_browser_mode_is_rejected() -> None:
    try:
        resolve_create_session_mode(
            {"mode": "unknown"}, configured_default="proxy", personal_chrome_configured=False
        )
    except HTTPException as error:
        assert error.status_code == 400
    else:
        raise AssertionError("invalid browser mode must be rejected")
