import socket
import threading
import unittest
import webbrowser
from click.testing import CliRunner
from unittest.mock import patch

from cptr import cli as cli_module


class _ImmediateTimer:
    def __init__(self, _delay, callback):
        self.callback = callback

    def start(self):
        self.callback()


class CliBrowserLaunchTests(unittest.TestCase):
    def test_default_run_does_not_open_external_browser(self):
        opened_urls: list[str] = []

        with (
            patch("builtins.print"),
            patch.object(webbrowser, "open", opened_urls.append),
            patch.object(threading, "Timer", _ImmediateTimer),
            patch.object(socket, "create_connection", side_effect=OSError("not listening")),
            patch.object(cli_module.uvicorn, "run"),
        ):
            result = CliRunner().invoke(
                cli_module.cli,
                ["run", "--host", "127.0.0.1", "--port", "43125"],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(opened_urls, [])

    def test_fresh_server_still_opens_one_browser_tab(self):
        opened_urls: list[str] = []

        with (
            patch("builtins.print"),
            patch.object(webbrowser, "open", opened_urls.append),
            patch.object(threading, "Timer", _ImmediateTimer),
            patch.object(socket, "create_connection", side_effect=OSError("not listening")),
            patch.object(cli_module.uvicorn, "run"),
        ):
            cli_module.run.callback(
                host="0.0.0.0", port=43124, reload=False, headless=False, open_browser=True
            )

        self.assertEqual(len(opened_urls), 1)
        self.assertTrue(opened_urls[0].startswith("http://localhost:43124/?token="))

    def test_existing_server_does_not_open_duplicate_browser_tab(self):
        opened_urls: list[str] = []

        with (
            patch("builtins.print"),
            patch.object(webbrowser, "open", opened_urls.append),
            patch.object(threading, "Timer", _ImmediateTimer),
            patch.object(socket, "create_connection"),
            patch.object(cli_module.uvicorn, "run"),
        ):
            socket.create_connection.return_value.__enter__.return_value = object()
            cli_module.run.callback(
                host="127.0.0.1", port=43123, reload=False, headless=False, open_browser=True
            )

        self.assertEqual(opened_urls, [])


if __name__ == "__main__":
    unittest.main()
