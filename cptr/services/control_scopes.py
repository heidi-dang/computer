"""Control API-key scope contracts and normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable


DEFAULT_CONTROL_SCOPES = (
    "workspace:read",
    "memory:read",
    "task:read",
    "task:write",
    "autonomous:run",
    "git:read",
    "coding:read",
    "coding:write",
    "command:execute",
    "mcp:traffic:write",
    "mcp:activity:write",
    "mcp:diagnostics:write",
)
CAPABILITY_CONTROL_SCOPES = (
    "capability:read",
    "capability:write",
    "capability:execute",
)
OPTIONAL_CONTROL_SCOPES = (
    "command:external",
    *CAPABILITY_CONTROL_SCOPES,
)
ALLOWED_CONTROL_SCOPES = frozenset((*DEFAULT_CONTROL_SCOPES, *OPTIONAL_CONTROL_SCOPES))


class InvalidControlScopes(ValueError):
    """Raised when an API-key scope set violates the public control contract."""


def normalize_control_scopes(
    requested_scopes: Iterable[str] | None,
    *,
    use_defaults: bool = False,
) -> list[str]:
    """Strip, deduplicate, and validate one control API-key scope set."""
    if requested_scopes is None:
        requested_scopes = DEFAULT_CONTROL_SCOPES if use_defaults else ()

    scopes = list(
        dict.fromkeys(
            scope.strip() for scope in requested_scopes if isinstance(scope, str) and scope.strip()
        )
    )
    if not scopes:
        raise InvalidControlScopes("at least one API-key scope is required")

    unknown_scopes = sorted(set(scopes) - ALLOWED_CONTROL_SCOPES)
    if unknown_scopes:
        raise InvalidControlScopes(f"unsupported API-key scope(s): {', '.join(unknown_scopes)}")
    return scopes


def set_capability_os_scopes(scopes: Iterable[str], enabled: bool) -> list[str]:
    """Toggle the complete Capability OS scope bundle while preserving other authority."""
    normalized = normalize_control_scopes(scopes)
    capability_scopes = set(CAPABILITY_CONTROL_SCOPES)
    if enabled:
        return normalize_control_scopes((*normalized, *CAPABILITY_CONTROL_SCOPES))
    return normalize_control_scopes(scope for scope in normalized if scope not in capability_scopes)
