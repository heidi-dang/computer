"""cptr API routers."""

from cptr.routers.admin import router as admin_router
from cptr.routers.audio import router as audio_router
from cptr.routers.auth import router as auth_router
from cptr.routers.automations import router as automations_router
from cptr.routers.automations_extended import router as automations_extended_router
from cptr.routers.benchmarks import router as benchmarks_router
from cptr.routers.bridge import router as bridge_router
from cptr.routers.bridge import webhook_router
from cptr.routers.browser import router as browser_router
from cptr.routers.browser_extended import router as browser_extended_router
from cptr.routers.browser_device import router as browser_device_router
from cptr.routers.chat import router as chat_router
from cptr.routers.chat_extended import router as chat_extended_router
from cptr.routers.coding import router as coding_router
from cptr.routers.control import router as control_router
from cptr.routers.control_stream import router as control_stream_router
from cptr.routers.events import router as events_router
from cptr.routers.factory import router as factory_router
from cptr.routers.files import router as files_router
from cptr.routers.gateway import router as gateway_router
from cptr.routers.gateway_extended import router as gateway_extended_router
from cptr.routers.git import router as git_router
from cptr.routers.images import router as images_router
from cptr.routers.mcp import router as mcp_router
from cptr.routers.memory import router as memory_router
from cptr.routers.memory_extended import router as memory_extended_router
from cptr.routers.notifications import router as notifications_router
from cptr.routers.search import router as search_router
from cptr.routers.search_extended import router as search_extended_router
from cptr.routers.skills import router as skills_router
from cptr.routers.skills_extended import router as skills_extended_router
from cptr.routers.state import router as state_router
from cptr.routers.system import router as system_router
from cptr.routers.terminal import router as terminal_router
from cptr.routers.terminal_extended import router as terminal_extended_router
from cptr.routers.workspace import router as workspace_router
from cptr.routers.workspace_extended import router as workspace_extended_router
from cptr.routers.workbench import router as workbench_router

__all__ = [
    "admin_router",
    "audio_router",
    "auth_router",
    "automations_router",
    "automations_extended_router",
    "benchmarks_router",
    "bridge_router",
    "browser_router",
    "browser_extended_router",
    "browser_device_router",
    "chat_router",
    "chat_extended_router",
    "coding_router",
    "control_router",
    "control_stream_router",
    "events_router",
    "factory_router",
    "files_router",
    "gateway_router",
    "gateway_extended_router",
    "git_router",
    "images_router",
    "mcp_router",
    "memory_router",
    "memory_extended_router",
    "notifications_router",
    "search_router",
    "search_extended_router",
    "skills_router",
    "skills_extended_router",
    "state_router",
    "system_router",
    "terminal_router",
    "terminal_extended_router",
    "webhook_router",
    "workspace_router",
    "workspace_extended_router",
    "workbench_router",
]
