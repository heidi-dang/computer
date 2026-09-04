"""Versioned browser-device message contract."""

PROTOCOL_VERSION = 1

BROWSER_ACTIONS = frozenset(
    {
        "status",
        "attach",
        "detach",
        "list_tabs",
        "get_tab",
        "activate_tab",
        "open_tab",
        "close_tab",
        "duplicate_tab",
        "list_windows",
        "new_window",
        "focus_window",
        "navigate",
        "back",
        "forward",
        "reload",
        "stop",
        "wait_for_navigation",
        "snapshot",
        "screenshot",
        "get_text",
        "get_html",
        "get_attribute",
        "get_url",
        "get_title",
        "find",
        "click",
        "double_click",
        "right_click",
        "hover",
        "type",
        "fill",
        "clear",
        "press_key",
        "key_down",
        "key_up",
        "scroll",
        "drag",
        "select_option",
        "check",
        "uncheck",
        "focus",
        "evaluate",
        "wait_for",
        "handle_dialog",
        "print_pdf",
        "download",
        "list_downloads",
        "cancel_download",
        "network_enable",
        "network_events",
        "console",
    }
)

MUTATING_BROWSER_ACTIONS = frozenset(
    {
        "attach",
        "detach",
        "activate_tab",
        "open_tab",
        "close_tab",
        "duplicate_tab",
        "new_window",
        "focus_window",
        "navigate",
        "back",
        "forward",
        "reload",
        "stop",
        "click",
        "double_click",
        "right_click",
        "hover",
        "type",
        "fill",
        "clear",
        "press_key",
        "key_down",
        "key_up",
        "scroll",
        "drag",
        "select_option",
        "check",
        "uncheck",
        "focus",
        "evaluate",
        "handle_dialog",
        "print_pdf",
        "download",
        "cancel_download",
        "network_enable",
    }
)

WIRE_BROWSER_MODES = frozenset(
    {"DISCONNECTED", "OBSERVING", "AGENT_CONTROL", "HANDOFF_REQUIRED", "HUMAN_CONTROL"}
)


def action_mutates_browser(action: str) -> bool:
    return action in MUTATING_BROWSER_ACTIONS


def wire_browser_mode(state: str) -> str | None:
    normalized = str(state).upper()
    return normalized if normalized in WIRE_BROWSER_MODES else None
