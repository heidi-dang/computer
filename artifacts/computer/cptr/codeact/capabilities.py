"""Read-only CodeAct adapters over CPTR's existing tool implementations."""

from __future__ import annotations

from typing import Any

from cptr.codeact.repl import ReadOnlyCapabilitySDK
from cptr.utils.tools import execute_tool


READ_ONLY_TOOL_MAP = {
    "files.read": "read_file",
    "files.list": "list_directory",
    "files.search": "search_files",
}


def sdk_from_tool_context(context: dict[str, Any]) -> ReadOnlyCapabilitySDK:
    """Build a capability SDK from the already-authorized native tool context.

    This adapter intentionally passes through CPTR's normal `execute_tool`
    policy, workspace, identity, and tool guard rather than reimplementing
    filesystem behavior. Git/browser adapters are added only when their
    existing read-only boundary is supplied by the caller.
    """

    async def invoke(capability: str, **kwargs: Any) -> Any:
        tool_name = READ_ONLY_TOOL_MAP.get(capability)
        if tool_name is None:
            raise PermissionError(f"capability not mapped: {capability}")
        native_context = dict(context)
        native_context["allowed_tool_names"] = frozenset(
            set(native_context.get("allowed_tool_names") or ()) | {tool_name}
        )
        result = await execute_tool(tool_name, kwargs, native_context)
        if isinstance(result, str) and result.startswith("Error"):
            raise PermissionError(result)
        return {"text": result}

    return ReadOnlyCapabilitySDK.from_handlers(
        {
            capability: (lambda capability=capability, **kwargs: invoke(capability, **kwargs))
            for capability in READ_ONLY_TOOL_MAP
        }
    )
