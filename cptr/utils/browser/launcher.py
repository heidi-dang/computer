"""Chrome/Chromium discovery and auto-launch.

Finds a running Chrome instance or launches one headless with a debug port.
Supports macOS and Linux. Called automatically when browser tools are invoked.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Common Chrome/Chromium binary paths by platform
_CHROME_PATHS_MACOS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

_CHROME_PATHS_LINUX = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge",
]

_CHROME_PATHS_WINDOWS = [
    ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
    ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
    ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
    ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
    ("LOCALAPPDATA", "BraveSoftware/Brave-Browser/Application/brave.exe"),
]

# Track browser processes launched by CPTR so shutdown never targets arbitrary user Chrome.
_launched_process: asyncio.subprocess.Process | None = None
_user_data_dir: str | None = None
_managed_process: asyncio.subprocess.Process | None = None
_managed_user_data_dir: str | None = None
_managed_base_url: str | None = None
_managed_lock = asyncio.Lock()


def _managed_browser_startup_timeout_seconds() -> float:
    """Return the bounded managed-Chrome readiness deadline.

    Hosted CI runners can occasionally take longer than the former fixed ten-second
    window to publish their local CDP endpoint. Operators may tune the deadline,
    but it remains bounded so a broken browser cannot stall requests indefinitely.
    """
    raw = os.environ.get("CPTR_MANAGED_BROWSER_STARTUP_TIMEOUT_SECONDS", "20")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 20.0
    return min(60.0, max(5.0, value))


def find_browser() -> str | None:
    """Find a compatible Chrome-family browser without launching it."""
    import platform

    system = platform.system()
    if system == "Darwin":
        for path in _CHROME_PATHS_MACOS:
            if Path(path).exists():
                return path
        # Also check PATH
        for name in ("google-chrome", "chromium"):
            found = shutil.which(name)
            if found:
                return found
    elif system == "Windows":
        for env_name, suffix in _CHROME_PATHS_WINDOWS:
            root = os.environ.get(env_name)
            if root and (candidate := Path(root) / suffix).exists():
                return str(candidate)
        for name in ("chrome", "msedge", "brave"):
            if found := shutil.which(name):
                return found
    else:
        for name in _CHROME_PATHS_LINUX:
            found = shutil.which(name)
            if found:
                return found

    return None


async def _probe_cdp(base_url: str) -> bool:
    """Check if a CDP endpoint is responding."""
    import httpx

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.get(f"{base_url}/json/version", timeout=3)
            data = resp.json()
            logger.info(
                "Found Chrome %s at %s",
                data.get("Browser", "unknown"),
                base_url,
            )
            return True
    except Exception:
        return False


async def ensure_browser(port: int = 9222) -> str:
    """Ensure a Chrome instance is available for CDP connection.

    1. Check if CDP is already available at the configured URL
    2. If not, find and launch Chrome/Chromium headless
    3. Return the CDP base URL

    Called automatically when any browser tool is invoked.
    """
    global _launched_process, _user_data_dir

    base_url = f"http://localhost:{port}"

    # 1. Check if already running
    if await _probe_cdp(base_url):
        return base_url

    # 2. Find Chrome binary
    chrome_path = find_browser()
    if not chrome_path:
        raise RuntimeError(
            "No Chrome or Chromium found. Install Google Chrome, Chromium, or Brave, "
            "or set browser.cdp_url to point to a running instance."
        )

    # 3. Launch headless with debug port
    _user_data_dir = tempfile.mkdtemp(prefix="cptr-browser-")

    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-translate",
        "--disable-extensions",
        f"--user-data-dir={_user_data_dir}",
    ]
    if Path("/.dockerenv").exists():
        args.append("--no-sandbox")
    args.append("about:blank")

    logger.info("Launching Chrome: %s", " ".join(args[:3]))

    _launched_process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    # Wait for CDP to become available
    for _ in range(20):
        await asyncio.sleep(0.5)
        if await _probe_cdp(base_url):
            logger.info("Chrome launched successfully on port %d", port)
            return base_url

    raise RuntimeError(
        f"Chrome launched but CDP not responding on port {port} after 10s. Binary: {chrome_path}"
    )


async def ensure_managed_browser() -> str:
    """Launch or reuse CPTR's isolated automation Chrome instance.

    Unlike ``ensure_browser()``, this never attaches to an arbitrary pre-existing
    CDP endpoint. It owns a temporary profile and an ephemeral loopback debug port
    so MCP browser control cannot inherit the user's normal Chrome cookies/profile.
    """
    global _managed_process, _managed_user_data_dir, _managed_base_url

    async with _managed_lock:
        if (
            _managed_process is not None
            and _managed_process.returncode is None
            and _managed_base_url
            and await _probe_cdp(_managed_base_url)
        ):
            return _managed_base_url

        if _managed_process is not None and _managed_process.returncode is None:
            try:
                _managed_process.terminate()
                await asyncio.wait_for(_managed_process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    _managed_process.kill()
                except ProcessLookupError:
                    pass
        _managed_process = None
        _managed_base_url = None
        if _managed_user_data_dir:
            shutil.rmtree(_managed_user_data_dir, ignore_errors=True)
            _managed_user_data_dir = None

        chrome_path = find_browser()
        if not chrome_path:
            raise RuntimeError(
                "No Chrome or Chromium found. Install Google Chrome, Chromium, Brave, or Edge."
            )

        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = int(sock.getsockname()[1])
        base_url = f"http://127.0.0.1:{port}"
        _managed_user_data_dir = tempfile.mkdtemp(prefix="cptr-managed-browser-")
        args = [
            chrome_path,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-translate",
            "--disable-extensions",
            f"--user-data-dir={_managed_user_data_dir}",
            "about:blank",
        ]
        if Path("/.dockerenv").exists():
            args.insert(-1, "--no-sandbox")

        _managed_process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        startup_timeout = _managed_browser_startup_timeout_seconds()
        deadline = asyncio.get_running_loop().time() + startup_timeout
        while True:
            await asyncio.sleep(0.5)
            if _managed_process.returncode is not None:
                break
            if await _probe_cdp(base_url):
                _managed_base_url = base_url
                return base_url
            if asyncio.get_running_loop().time() >= deadline:
                break

        returncode = _managed_process.returncode
        try:
            _managed_process.terminate()
        except ProcessLookupError:
            pass
        raise RuntimeError(
            "Managed Chrome did not expose its local CDP endpoint within "
            f"{startup_timeout:.1f}s (binary: {chrome_path}, returncode: {returncode})"
        )


async def shutdown_browser() -> None:
    """Kill Chrome processes launched by CPTR. Called on app shutdown."""
    global \
        _launched_process, \
        _user_data_dir, \
        _managed_process, \
        _managed_user_data_dir, \
        _managed_base_url

    if _launched_process and _launched_process.returncode is None:
        logger.info("Shutting down launched Chrome (pid %d)", _launched_process.pid)
        try:
            _launched_process.terminate()
            await asyncio.wait_for(_launched_process.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                _launched_process.kill()
            except ProcessLookupError:
                pass
        _launched_process = None

    if _managed_process and _managed_process.returncode is None:
        logger.info("Shutting down managed automation Chrome (pid %d)", _managed_process.pid)
        try:
            _managed_process.terminate()
            await asyncio.wait_for(_managed_process.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                _managed_process.kill()
            except ProcessLookupError:
                pass
    _managed_process = None
    _managed_base_url = None
    if _managed_user_data_dir:
        shutil.rmtree(_managed_user_data_dir, ignore_errors=True)
        _managed_user_data_dir = None

    # Clean up temp profile
    if _user_data_dir:
        import shutil as sh

        try:
            sh.rmtree(_user_data_dir, ignore_errors=True)
        except Exception:
            pass
        _user_data_dir = None
