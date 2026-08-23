"""Browser-Use cloud provider — LLM-driven browser tasks via REST API.

Pure httpx, no SDK dependency.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.browser-use.com"


async def browse(
    task: str,
    api_key: str,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    """Run a natural language browser task and return the result.

    Uses POST /v1/run.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"task": task}

    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(
            f"{base_url.rstrip('/')}/v1/run",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

    result = data.get("result", data.get("output", ""))

    if not result:
        return f"Browser-Use returned no result for task: {task}"

    # Trim if needed
    if isinstance(result, str) and len(result) > 50_000:
        result = result[:50_000] + "\n\n[... truncated]"

    return str(result)
