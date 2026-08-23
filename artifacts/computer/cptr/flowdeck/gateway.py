"""The single non-authoritative FlowDeck observation boundary."""

from __future__ import annotations

import json
import logging
from typing import Any

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import ShadowDiagnostic
from cptr.flowdeck.router import shadow_route

logger = logging.getLogger(__name__)


def observe_request(
    *,
    content: str,
    model_id: str = "",
    user_id: str | None = None,
    workspace: str | None = None,
) -> ShadowDiagnostic | None:
    """Observe a request without ever becoming its execution owner.

    The disabled branch intentionally returns before constructing the route or
    diagnostics. The result is returned for tests and development callers; the
    CPTR request path discards it.
    """
    config = FlowDeckConfig.from_env()
    if not config.enabled or config.mode.value == "off":
        return None

    try:
        diagnostic = shadow_route(content, model_id, config)
        _log_bounded_diagnostic(diagnostic, user_id=user_id, workspace=workspace, limit=config.max_diagnostic_chars)
        return diagnostic
    except Exception:
        # Shadow observation is advisory: it can never fail the native CPTR request.
        logger.debug("FlowDeck shadow observation failed", exc_info=True)
        return None


def _log_bounded_diagnostic(
    diagnostic: ShadowDiagnostic,
    *,
    user_id: str | None,
    workspace: str | None,
    limit: int,
) -> None:
    payload: dict[str, Any] = {
        "mode": diagnostic.mode.value,
        "strategy": diagnostic.route.strategy.value,
        "specialists": diagnostic.route.specialist_ids,
        "governance": [
            {"capability": item.capability.value, "verdict": item.verdict.value}
            for item in diagnostic.governance
        ],
        "warnings": diagnostic.warnings,
        "user_present": bool(user_id),
        "workspace_present": bool(workspace),
    }
    # No content, provider payloads, credentials, or workspace paths are logged.
    logger.debug("FlowDeck shadow diagnostic: %s", json.dumps(payload)[:limit])