import functools
import json
import re
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cptr.utils.browser.cdp import CDPClient
from cptr.utils.browser.launcher import ensure_managed_browser, find_browser, shutdown_browser


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


@unittest.skipUnless(find_browser(), "Chrome-family browser is not installed")
class ManagedChromeLiveQualificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fixture = Path(self.temp.name) / "index.html"
        fixture.write_text(
            """<!doctype html>
<html><head><title>CPTR browser qualification</title></head>
<body>
  <button id="go" onclick="document.getElementById('out').textContent='clicked'; document.title='clicked'">Go</button>
  <label>Name <input id="name" aria-label="Name" value=""></label>
  <p id="out">idle</p>
  <input id="secret" type="password" aria-label="Password" value="super-secret-value">
</body></html>
""",
            encoding="utf-8",
        )
        handler = functools.partial(QuietHandler, directory=self.temp.name)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/index.html"

    async def asyncTearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()
        await shutdown_browser()

    async def test_real_managed_chrome_navigation_interaction_and_screenshot(self):
        cdp_url = await ensure_managed_browser()
        self.assertTrue(cdp_url.startswith("http://127.0.0.1:"))
        client = await CDPClient.connect(cdp_url)
        try:
            navigation = await client.navigate(self.url)
            self.assertEqual(navigation["title"], "CPTR browser qualification")

            snapshot = await client.snapshot()
            self.assertIn("Go", snapshot)
            self.assertIn("Name", snapshot)
            self.assertNotIn("super-secret-value", snapshot)

            button = re.search(r"\[button (@e\d+)\] Go", snapshot)
            textbox = re.search(r"\[textbox (@e\d+)\] Name", snapshot)
            self.assertIsNotNone(button, snapshot)
            self.assertIsNotNone(textbox, snapshot)

            await client.click(button.group(1))
            await client.type_text(textbox.group(1), "hello")
            await client.press_key("!", [])

            state = json.loads(
                await client.evaluate(
                    "JSON.stringify({title:document.title,value:document.getElementById('name').value,out:document.getElementById('out').textContent})"
                )
            )
            self.assertEqual(state, {"title": "clicked", "value": "hello!", "out": "clicked"})

            png = await client.screenshot(width=800, height=600)
            self.assertGreater(len(png), 1000)
            self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        finally:
            await client.close()


if __name__ == "__main__":
    unittest.main()
