"""MCP client identity policy for the active ChatGPT deployment."""

ACTIVE_MCP_CLIENT_ID = "chatgpt"
_CHATGPT_SESSION_PREFIX = "chatgpt-session-"


def is_active_mcp_client_id(value: object) -> bool:
    """Return whether a telemetry client ID belongs to the active ChatGPT client."""
    if not isinstance(value, str):
        return False
    client_id = value.strip().lower()
    return client_id == ACTIVE_MCP_CLIENT_ID or (
        client_id.startswith(_CHATGPT_SESSION_PREFIX)
        and len(client_id) > len(_CHATGPT_SESSION_PREFIX)
    )
