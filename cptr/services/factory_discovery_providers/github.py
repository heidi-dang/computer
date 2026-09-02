"""GitHub repository metadata discovery for Dark Factory capability research."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import httpx

from cptr.services.factory_discovery import DiscoveryCandidate

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


class GitHubDiscoveryProvider:
    name = "github"

    def __init__(
        self,
        *,
        api_base_url: str = "https://api.github.com",
        token: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._token = token
        self._timeout = timeout_seconds
        self._transport = transport

    async def discover(self, query: str, *, limit: int) -> list[DiscoveryCandidate]:
        limit = max(1, min(int(limit), 100))
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "cptr-dark-factory",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        async with httpx.AsyncClient(
            base_url=self._api_base_url,
            headers=headers,
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = await client.get(
                "/search/repositories",
                params={"q": query, "per_page": limit, "sort": "updated", "order": "desc"},
            )
            response.raise_for_status()
            payload = response.json()
            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                return []

            candidates: list[DiscoveryCandidate] = []
            for item in items[:limit]:
                if not isinstance(item, dict):
                    continue
                full_name = str(item.get("full_name") or "").strip()
                origin_uri = str(item.get("html_url") or "").strip()
                if not full_name or not origin_uri:
                    continue
                default_branch = str(item.get("default_branch") or "").strip()
                pin = await self._resolve_pin(client, full_name, default_branch)
                source_uri = origin_uri
                if pin:
                    source_uri = f"https://codeload.github.com/{full_name}/tar.gz/{pin}"
                candidates.append(
                    DiscoveryCandidate.create(
                        provider=self.name,
                        candidate_type="repository",
                        name=full_name,
                        version=None,
                        origin_uri=origin_uri,
                        source_uri=source_uri,
                        pinned_version_or_commit=pin,
                        capabilities=("repository-source", "skill-source"),
                        permissions=("workspace:read",),
                        metadata={
                            "description": item.get("description"),
                            "default_branch": default_branch or None,
                            "stargazers_count": item.get("stargazers_count"),
                            "updated_at": item.get("updated_at"),
                            "archived": bool(item.get("archived", False)),
                            "fork": bool(item.get("fork", False)),
                            "license": (
                                item.get("license", {}).get("spdx_id")
                                if isinstance(item.get("license"), dict)
                                else None
                            ),
                        },
                    )
                )
            return candidates

    async def _resolve_pin(
        self,
        client: httpx.AsyncClient,
        full_name: str,
        default_branch: str,
    ) -> str | None:
        if not default_branch:
            return None
        try:
            path = f"/repos/{full_name}/commits/{quote(default_branch, safe='')}"
            response = await client.get(path)
            response.raise_for_status()
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            return None
        sha = str(payload.get("sha") or "") if isinstance(payload, dict) else ""
        return sha.lower() if _COMMIT_RE.fullmatch(sha) else None
