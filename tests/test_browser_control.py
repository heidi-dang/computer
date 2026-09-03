import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException
from pydantic import ValidationError

from cptr.routers.coding import (
    MAX_BROWSER_SNAPSHOT_CHARS,
    BrowserControlRequest,
    _bounded_browser_snapshot,
    _settled_browser_snapshot,
    _validate_browser_url,
    router as coding_router,
)
from cptr.utils.browser.cdp import CDPClient, _normalize_ref
from cptr.utils.browser.session import BrowserSessionManager


class BrowserControlValidationTests(unittest.TestCase):
    def test_control_router_registers_one_workspace_browser_endpoint(self):
        browser_routes = [
            route
            for route in coding_router.routes
            if route.path == "/api/control/v1/workspaces/{workspace_id}/browser"
        ]
        self.assertEqual(len(browser_routes), 1)
        self.assertIn("POST", browser_routes[0].methods)

    def request(self, scopes=()):
        return SimpleNamespace(state=SimpleNamespace(control_scopes=set(scopes)))

    def test_loopback_http_is_allowed_without_external_scope(self):
        value = _validate_browser_url(
            self.request(), "http://127.0.0.1:8765/test", allow_network=False
        )
        self.assertEqual(value, "http://127.0.0.1:8765/test")

    def test_localhost_is_allowed_without_external_scope(self):
        value = _validate_browser_url(
            self.request(), "http://localhost:8765/test", allow_network=False
        )
        self.assertEqual(value, "http://localhost:8765/test")

    def test_external_url_requires_explicit_network_permission(self):
        with self.assertRaises(HTTPException) as raised:
            _validate_browser_url(self.request(), "https://example.com", allow_network=False)
        self.assertEqual(raised.exception.status_code, 403)

    def test_external_url_requires_external_scope_even_when_network_allowed(self):
        with self.assertRaises(HTTPException) as raised:
            _validate_browser_url(self.request(), "https://example.com", allow_network=True)
        self.assertEqual(raised.exception.status_code, 403)

    def test_external_url_with_external_scope_is_allowed(self):
        value = _validate_browser_url(
            self.request({"command:external"}),
            "https://example.com/path",
            allow_network=True,
        )
        self.assertEqual(value, "https://example.com/path")

    def test_dangerous_scheme_is_rejected(self):
        for value in ("file:///tmp/test.html", "javascript:alert(1)", "data:text/html,test"):
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                _validate_browser_url(self.request(), value, allow_network=False)
            self.assertEqual(raised.exception.status_code, 422)

    def test_embedded_credentials_are_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            _validate_browser_url(
                self.request({"command:external"}),
                "https://user:secret@example.com/path",
                allow_network=True,
            )
        self.assertEqual(raised.exception.status_code, 422)

    def test_snapshot_is_bounded(self):
        snapshot, truncated = _bounded_browser_snapshot("x" * (MAX_BROWSER_SNAPSHOT_CHARS + 50))
        self.assertTrue(truncated)
        self.assertIn("[Browser snapshot truncated by CPTR.]", snapshot)
        self.assertLess(len(snapshot), MAX_BROWSER_SNAPSHOT_CHARS + 100)

    def test_request_rejects_unknown_action(self):
        with self.assertRaises(ValidationError):
            BrowserControlRequest(action="evaluate")

    def test_request_bounds_text_and_viewport(self):
        with self.assertRaises(ValidationError):
            BrowserControlRequest(action="type", text="x" * 20_001)
        with self.assertRaises(ValidationError):
            BrowserControlRequest(action="screenshot", width=100, height=100)

    def test_accessibility_refs_accept_rendered_and_compact_forms(self):
        self.assertEqual(_normalize_ref("@e33"), "@e33")
        self.assertEqual(_normalize_ref("e33"), "@e33")
        self.assertEqual(_normalize_ref("33"), "@e33")


class BrowserSnapshotSettlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_snapshot_retries_transient_empty_page(self):
        client = SimpleNamespace(snapshot=AsyncMock(side_effect=["[empty page]", "[button @e1] Ready"]))
        result = await _settled_browser_snapshot(client, settle_seconds=0.5)
        self.assertEqual(result, "[button @e1] Ready")
        self.assertEqual(client.snapshot.await_count, 2)


class BrowserSessionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_has_does_not_create_and_removes_closed_client(self):
        manager = BrowserSessionManager()
        self.assertFalse(manager.has("missing"))
        closed = SimpleNamespace(is_closed=lambda: True)
        manager._sessions["closed"] = closed
        manager._last_used["closed"] = 1.0
        self.assertFalse(manager.has("closed"))
        self.assertNotIn("closed", manager._sessions)


class CDPNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigate_fails_closed_when_cdp_reports_network_error(self):
        client = CDPClient(SimpleNamespace(), "target")
        client._send = AsyncMock(return_value={"errorText": "net::ERR_NAME_NOT_RESOLVED"})
        client._recv_json = AsyncMock()

        with self.assertRaisesRegex(RuntimeError, "ERR_NAME_NOT_RESOLVED"):
            await client.navigate("https://example.com")

        client._recv_json.assert_not_awaited()


class CDPPressKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_press_key_dispatches_bounded_key_down_and_up(self):
        client = CDPClient(SimpleNamespace(), "target")
        client._send = AsyncMock(return_value={})

        await client.press_key("Enter", ["Control", "Shift"])

        self.assertEqual(client._send.await_count, 2)
        first = client._send.await_args_list[0].args
        second = client._send.await_args_list[1].args
        self.assertEqual(first[0], "Input.dispatchKeyEvent")
        self.assertEqual(first[1]["type"], "keyDown")
        self.assertEqual(first[1]["key"], "Enter")
        self.assertEqual(first[1]["modifiers"], 10)
        self.assertEqual(second[1]["type"], "keyUp")

    async def test_press_key_rejects_unknown_modifier(self):
        client = CDPClient(SimpleNamespace(), "target")
        client._send = AsyncMock(return_value={})
        with self.assertRaises(ValueError):
            await client.press_key("a", ["CapsLock"])
        client._send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
