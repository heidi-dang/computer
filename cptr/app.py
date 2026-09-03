import asyncio
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from cptr.routers import (
    admin_router,
    audio_router,
    auth_router,
    automations_router,
    automations_extended_router,
    benchmarks_router,
    bridge_router,
    browser_router,
    browser_extended_router,
    chat_router,
    chat_extended_router,
    coding_router,
    control_router,
    control_stream_router,
    events_router,
    factory_router,
    files_router,
    gateway_router,
    gateway_extended_router,
    git_router,
    images_router,
    mcp_router,
    memory_router,
    memory_extended_router,
    notifications_router,
    search_router,
    search_extended_router,
    skills_router,
    skills_extended_router,
    state_router,
    system_router,
    terminal_router,
    terminal_extended_router,
    webhook_router,
    workspace_router,
    workspace_extended_router,
    workbench_router,
)
from cptr.utils.config import check_access, load_config
from cptr.utils.db import init_db

START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    from cptr.utils.logger import setup_logging

    setup_logging()

    # Use OS certificate store (Windows CertStore, macOS Keychain, etc.)
    # instead of the bundled certifi CA bundle — fixes #31.
    import logging as _logging
    import truststore

    truststore.inject_into_ssl()
    _logging.getLogger(__name__).info("truststore: using system certificate store")

    await init_db()

    from cptr.env import FACTORY_RUN_LEASE_MS
    from cptr.services.factory_control import FactoryControlService
    from cptr.services.factory_production import FactoryProductionRunner
    from cptr.services.factory_runtime import FactoryRuntime
    from cptr.services.factory_store import SqlFactoryStore

    factory_store = SqlFactoryStore()
    app.state.factory_runtime = FactoryRuntime(
        store=factory_store,
        owner_token=f"factory-runtime-{uuid.uuid4().hex}",
        lease_ms=FACTORY_RUN_LEASE_MS,
    )
    app.state.factory_recovered_run_ids = await app.state.factory_runtime.recover_active_runs()
    app.state.factory_control_service = FactoryControlService(store=factory_store)
    app.state.factory_production_runner = FactoryProductionRunner(
        store=factory_store,
        lease_ms=FACTORY_RUN_LEASE_MS,
    )
    app.state.factory_scheduled_run_ids = await app.state.factory_production_runner.schedule_active()

    from cptr.services.live_events import live_event_hub
    from cptr.services.runtime_metrics import event_loop_lag_worker
    from cptr.utils.tools import command_session_reaper_loop, start_command_session_manager

    await live_event_hub.start()
    start_command_session_manager()
    app.state.command_session_reaper_task = asyncio.create_task(
        command_session_reaper_loop(), name="cptr-command-session-reaper"
    )
    app.state.event_loop_lag_task = asyncio.create_task(
        event_loop_lag_worker(), name="cptr-event-loop-lag"
    )

    from cptr.routers.control import recover_monitors

    await recover_monitors(app)
    app.state.MODELS = {}
    from cptr.env import STARTUP_TOKEN

    app.state.startup_token = STARTUP_TOKEN
    # Reconcile stuck chat state from prior crash/restart
    from cptr.env import ENABLE_CHAT_RECONCILE_ON_STARTUP

    if ENABLE_CHAT_RECONCILE_ON_STARTUP:
        from cptr.utils.chat_task import reconcile_chat_state

        await reconcile_chat_state()

    from cptr.routers.chat import warm_model_cache

    await warm_model_cache(app.state)

    # Start automation scheduler
    from cptr.utils.automations import scheduler_worker_loop

    app.state.scheduler_task = asyncio.create_task(scheduler_worker_loop(app))

    from cptr.utils.timers import recover_timers, timer_worker_loop

    await recover_timers()
    app.state.timer_task = asyncio.create_task(timer_worker_loop(app))

    # Start messaging bots
    from cptr.utils.bridge import BotManager

    app.state.bot_manager = BotManager(app)
    await app.state.bot_manager.start_all()

    try:
        yield
    finally:
        factory_runner = getattr(app.state, "factory_production_runner", None)
        if factory_runner is not None:
            await factory_runner.close()

        for monitor_task in getattr(app.state, "control_monitor_tasks", {}).values():
            monitor_task.cancel()
        for monitor_task in getattr(app.state, "control_monitor_tasks", {}).values():
            with suppress(asyncio.CancelledError):
                await monitor_task
        timer_task = getattr(app.state, "timer_task", None)
        if timer_task:
            timer_task.cancel()
            with suppress(asyncio.CancelledError):
                await timer_task

        scheduler_task = getattr(app.state, "scheduler_task", None)
        if scheduler_task:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task

        for task_name in ("command_session_reaper_task", "event_loop_lag_task"):
            background_task = getattr(app.state, task_name, None)
            if background_task:
                background_task.cancel()
                with suppress(asyncio.CancelledError):
                    await background_task

        bot_manager = getattr(app.state, "bot_manager", None)
        if bot_manager:
            await bot_manager.stop_all()
        try:
            from cptr.utils.async_subagents import cancel_all_async_subagents

            await cancel_all_async_subagents(reason="shutdown")
        except Exception:
            pass

        # Command sessions own process groups, log writers, and live-event
        # producers. Quiesce them before closing event durability.
        try:
            from cptr.utils.tools import shutdown_command_sessions

            await shutdown_command_sessions()
        except Exception:
            pass

        # Clean up browser sessions and launched Chrome used by agent tools.
        try:
            from cptr.utils.browser.session import session_manager
            from cptr.utils.browser.launcher import shutdown_browser
            from cptr.utils.browser.proxy import manager as browser_proxy_manager
            from cptr.utils.browser.viewer import manager as chrome_viewer_manager

            await chrome_viewer_manager.close_all()
            await browser_proxy_manager.close_all()
            await session_manager.close_all()
            await shutdown_browser()
        except Exception:
            pass
        # Language servers are CPTR-owned workspace subprocesses. Dispose all
        # sessions before lower-level process/event infrastructure is closed.
        try:
            from cptr.services.lsp_manager import lsp_manager

            await lsp_manager.shutdown_all()
        except Exception:
            pass

        # Clean up stdio MCP server processes
        try:
            from cptr.utils.mcp.stdio_manager import stdio_manager

            await stdio_manager.disconnect_all()
        except Exception:
            pass

        # FDX daemons are workspace-bound native processes owned by this CPTR
        # execution process. Shut them down before event/logging infrastructure.
        try:
            from cptr.services.fdx_intelligence import service as fdx_intelligence_service

            await fdx_intelligence_service.close_all()
        except Exception:
            pass

        try:
            from cptr.services.live_events import live_event_hub

            await live_event_hub.close()
        except Exception:
            pass
        try:
            from cptr.utils.logger import complete_logging

            await complete_logging()
        except Exception:
            pass


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def performance_metrics_middleware(request: Request, call_next):
    """Record bounded request latency samples without affecting responses."""
    from cptr.services.runtime_metrics import runtime_metrics

    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        runtime_metrics.observe_request(
            (time.perf_counter() - started) * 1000.0,
            status_code=status_code,
        )


# Auth middleware
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    # Skip auth for: auth endpoints, health, static assets, HTML pages
    if (
        path.startswith("/api/auth")
        or path.startswith("/api/health")
        or path == "/api/config"
        or path == "/api/changelog"
        or path == "/manifest.json"
        or (
            request.method == "POST"
            and path
            in {
                "/api/mcp/traffic/events",
                "/api/mcp/activity/events",
                "/api/mcp/diagnostics/events",
            }
        )
    ):
        return await call_next(request)
    if (
        path.startswith("/_app/")
        or path.startswith("/v1/")
        or path.startswith("/api/control/v1")
        or not path.startswith("/api/")
    ):
        return await call_next(request)
    # GET /api/files/{id} is public (UUID is unguessable, <img src> can't send cookies)
    if request.method == "GET" and path.startswith("/api/files/"):
        return await call_next(request)

    client_host = request.client.host if request.client else "127.0.0.1"
    jwt_token = request.cookies.get("cptr_session")

    # Read trusted header (configurable, defaults to Remote-User)
    header_name = load_config().get("auth", {}).get("header", "Remote-User")
    remote_user = request.headers.get(header_name)

    auth = check_access(
        client_host=client_host,
        jwt_token=jwt_token,
        remote_user_header=remote_user,
    )
    if auth is None:
        return JSONResponse({"error": "unauthorized"}, 401)

    request.state.auth = auth
    return await call_next(request)


from cptr.env import AUDIT_EXCLUDED_PATHS, AUDIT_MAX_BODY_SIZE  # noqa: E402 - middleware is configured after app creation
from cptr.utils.audit import AuditLevel, AuditLoggingMiddleware, get_audit_level  # noqa: E402 - middleware is configured after app creation

audit_level = get_audit_level()
if audit_level != AuditLevel.NONE:
    app.add_middleware(
        AuditLoggingMiddleware,
        audit_level=audit_level,
        excluded_paths=AUDIT_EXCLUDED_PATHS,
        max_body_size=AUDIT_MAX_BODY_SIZE,
    )


# CORS middleware: uses CPTR_CORS_ALLOWED_ORIGINS env var (default "*").
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402 - middleware is configured after app creation
from cptr.env import CORS_ALLOWED_ORIGINS  # noqa: E402 - middleware is configured after app creation

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS if isinstance(CORS_ALLOWED_ORIGINS, list) else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Path normalization middleware (Windows: \ → / in JSON responses)
import platform  # noqa: E402 - platform-specific middleware is declared after app creation

if platform.system() == "Windows":
    import json as _json

    @app.middleware("http")
    async def normalize_paths_middleware(request: Request, call_next):
        response = await call_next(request)
        if response.headers.get("content-type", "").startswith("application/json") and hasattr(
            response, "body"
        ):
            try:
                body = b""
                async for chunk in response.body_iterator:
                    body += chunk if isinstance(chunk, bytes) else chunk.encode()
                text = body.decode("utf-8")
                # Replace backslashes that appear in JSON string values
                # (JSON already escapes \ as \\, so we replace \\\\ with /)
                data = _json.loads(text)
                _normalize_obj(data)
                new_body = _json.dumps(data, ensure_ascii=False).encode("utf-8")
                from starlette.responses import Response as StarletteResponse

                return StarletteResponse(
                    content=new_body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type="application/json",
                )
            except Exception:
                pass
        return response

    def _normalize_obj(obj):
        """Recursively replace backslashes with forward slashes in string values
        that look like file paths (contain :\\)."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and "\\" in v:
                    obj[k] = v.replace("\\", "/")
                elif isinstance(v, (dict, list)):
                    _normalize_obj(v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if isinstance(v, str) and "\\" in v:
                    obj[i] = v.replace("\\", "/")
                elif isinstance(v, (dict, list)):
                    _normalize_obj(v)


@app.get("/api/config")
async def get_config():
    """Public config for the frontend."""
    from cptr.utils.config import get_auth_mode, AuthMode, has_any_user
    from cptr.models import Config
    from importlib.metadata import version as pkg_version

    mode = get_auth_mode()
    needs_setup = False
    signup_enabled = False

    if mode == AuthMode.PASSWORD:
        needs_setup = not await has_any_user()
        if not needs_setup:
            signup_enabled = await Config.get("auth.signup_enabled") or False

    try:
        version = pkg_version("cptr")
    except Exception:
        version = "dev"

    return {
        "auth_mode": mode.value if hasattr(mode, "value") else str(mode),
        "needs_setup": needs_setup,
        "signup_enabled": signup_enabled,
        "version": version,
    }


# Routers
app.include_router(admin_router)
app.include_router(audio_router)
app.include_router(auth_router)
app.include_router(automations_router)
app.include_router(automations_extended_router)
app.include_router(benchmarks_router)
app.include_router(bridge_router)
app.include_router(browser_router)
app.include_router(browser_extended_router)
app.include_router(webhook_router)
app.include_router(chat_router)
app.include_router(chat_extended_router)
app.include_router(coding_router)
app.include_router(control_router)
app.include_router(control_stream_router)
app.include_router(events_router)
app.include_router(factory_router)
app.include_router(files_router)
app.include_router(gateway_router)
app.include_router(gateway_extended_router)
app.include_router(git_router)
app.include_router(images_router)
app.include_router(mcp_router)
app.include_router(memory_router)
app.include_router(memory_extended_router)
app.include_router(notifications_router)
app.include_router(search_router)
app.include_router(search_extended_router)
app.include_router(skills_router)
app.include_router(skills_extended_router)
app.include_router(state_router)
app.include_router(system_router)
app.include_router(terminal_router)
app.include_router(terminal_extended_router)
app.include_router(workspace_router)
app.include_router(workspace_extended_router)
app.include_router(workbench_router)


# Health / operational metrics
@app.get("/api/health")
@app.get("/api/health/live")
async def health():
    import os

    return {"status": "ok", "uptime_seconds": int(time.time() - START_TIME), "pid": os.getpid()}


@app.get("/api/health/ready")
async def health_ready():
    from cptr.utils.db import database_ready

    ready = await database_ready()
    payload = {"status": "ready" if ready else "not_ready"}
    return payload if ready else JSONResponse(payload, status_code=503)


@app.get("/api/metrics")
async def metrics(request: Request):
    """Admin-only bounded operational snapshot for performance/stability diagnosis."""
    from cptr.routers.admin import require_admin
    from cptr.services.api_keys import api_key_cache_stats
    from cptr.services.live_events import live_event_hub
    from cptr.services.runtime_metrics import runtime_metrics
    from cptr.utils.db import database_file_stats
    from cptr.utils.tools import command_session_metrics

    require_admin(request)
    snapshot = runtime_metrics.snapshot()
    snapshot["database"].update(database_file_stats())
    return {
        **snapshot,
        "commands": command_session_metrics(),
        "live_events": live_event_hub.stats(),
        "api_key_cache": api_key_cache_stats(),
    }


@app.get("/api/changelog")
async def get_changelog():
    """Return parsed CHANGELOG.md as structured JSON (max 5 versions)."""
    from cptr.utils.changelog import CHANGELOG

    return {key: CHANGELOG[key] for idx, key in enumerate(CHANGELOG) if idx < 5}


@app.get("/api/version/updates")
async def get_version_updates(request: Request):
    """Check GitHub for the latest release. Admin-only."""
    from cptr.routers.admin import require_admin
    from importlib.metadata import version as pkg_version

    require_admin(request)

    try:
        current = pkg_version("cptr")
    except Exception:
        current = "dev"

    try:
        import httpx

        async with httpx.AsyncClient(timeout=2) as client:
            r = await client.get(
                "https://api.github.com/repos/open-webui/computer/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
            r.raise_for_status()
            tag = r.json().get("tag_name", "")
            latest = tag.lstrip("v") if tag else current
            return {"current": current, "latest": latest}
    except Exception:
        return {"current": current, "latest": current}


# PWA manifest (backend-driven so each instance has its own identity)
@app.get("/manifest.json")
async def pwa_manifest():
    import socket
    from importlib.metadata import version as pkg_version

    try:
        hostname = socket.gethostname()
    except Exception:
        hostname = ""

    try:
        version = pkg_version("cptr")
    except Exception:
        version = "dev"

    name = f"cptr @ {hostname}" if hostname else "cptr"

    return {
        "id": "/",
        "name": name,
        "short_name": "cptr",
        "description": f"Your computer, from anywhere. v{version}",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "display_override": ["window-controls-overlay", "standalone"],
        "orientation": "any",
        "background_color": "#000000",
        "theme_color": "#000000",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {
                "src": "/icon-512.png",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
        "categories": ["developer", "productivity", "utilities"],
        "launch_handler": {"client_mode": ["navigate-existing", "auto"]},
        "handle_links": "preferred",
        "note_taking": {"new_note_url": "/?intent=newNote"},
        "shortcuts": [
            {
                "name": "New Chat",
                "short_name": "Chat",
                "url": "/?intent=newChat",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192"}],
            },
            {
                "name": "Open Workspace",
                "short_name": "Workspace",
                "url": "/?intent=openWorkspace",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192"}],
            },
            {
                "name": "New Note",
                "short_name": "Note",
                "url": "/?intent=newNote",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192"}],
            },
            {
                "name": "New Terminal",
                "short_name": "Terminal",
                "url": "/?intent=newTerminal",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192"}],
            },
            {
                "name": "Search",
                "short_name": "Search",
                "url": "/?intent=search",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192"}],
            },
        ],
        "share_target": {
            "action": "/?intent=share",
            "method": "POST",
            "enctype": "multipart/form-data",
            "params": {
                "title": "title",
                "text": "text",
                "url": "url",
                "files": [
                    {
                        "name": "files",
                        "accept": [
                            "text/*",
                            "application/pdf",
                            "image/*",
                            ".md",
                            ".py",
                            ".js",
                            ".ts",
                            ".tsx",
                            ".jsx",
                            ".svelte",
                            ".json",
                            ".yaml",
                            ".yml",
                            ".toml",
                            ".rs",
                            ".go",
                        ],
                    }
                ],
            },
        },
        "file_handlers": [
            {
                "action": "/?intent=importFiles",
                "accept": {
                    "text/*": [
                        ".txt",
                        ".md",
                        ".py",
                        ".js",
                        ".ts",
                        ".tsx",
                        ".jsx",
                        ".svelte",
                        ".css",
                        ".html",
                        ".json",
                        ".yaml",
                        ".yml",
                        ".toml",
                        ".rs",
                        ".go",
                        ".java",
                        ".c",
                        ".cpp",
                        ".h",
                        ".hpp",
                        ".sh",
                    ],
                    "application/pdf": [".pdf"],
                    "image/*": [".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"],
                },
            }
        ],
        "protocol_handlers": [
            {"protocol": "web+cptr", "url": "/?intent=%s"},
        ],
        "prefer_related_applications": False,
        "related_applications": [],
    }


# Frontend (unchanged)
FRONTEND_BUILD_DIR = Path(__file__).parent / "frontend" / "build"
if FRONTEND_BUILD_DIR.exists():
    app.mount(
        "/_app", StaticFiles(directory=str(FRONTEND_BUILD_DIR / "_app")), name="frontend-assets"
    )

    @app.get("/{full_path:path}")
    async def serve_spa(request: Request, full_path: str):
        file_path = FRONTEND_BUILD_DIR / full_path
        if full_path and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_BUILD_DIR / "index.html")


# Socket.IO: wraps the entire ASGI app
from cptr.socket.main import get_asgi_app  # noqa: E402 - Socket.IO wraps the fully configured ASGI app

application = get_asgi_app(app)
