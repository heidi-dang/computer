from fastapi import HTTPException

from cptr.cli import should_open_dashboard
from cptr.routers.browser import resolve_create_session_mode


def test_dashboard_opening_requires_explicit_opt_in() -> None:
    assert not should_open_dashboard(open_browser=False, headless=False)
    assert not should_open_dashboard(open_browser=True, headless=True)
    assert should_open_dashboard(open_browser=True, headless=False)


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
