"""Exact-package PyPI metadata discovery for Dark Factory capability research."""

from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from cptr.services.factory_discovery import DiscoveryCandidate

_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")


class PyPIRegistryDiscoveryProvider:
    name = "pypi_registry"

    def __init__(
        self,
        *,
        registry_base_url: str = "https://pypi.org",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._registry_base_url = registry_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def discover(self, query: str, *, limit: int) -> list[DiscoveryCandidate]:
        package_name = query.strip()
        if limit < 1 or not _PACKAGE_NAME_RE.fullmatch(package_name):
            return []
        async with httpx.AsyncClient(
            base_url=self._registry_base_url,
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
            headers={"User-Agent": "cptr-dark-factory", "Accept": "application/json"},
        ) as client:
            response = await client.get(f"/pypi/{quote(package_name, safe='')}/json")
            if response.status_code == 404:
                return []
            response.raise_for_status()
            payload = response.json()

        info = payload.get("info") if isinstance(payload, dict) else None
        urls = payload.get("urls") if isinstance(payload, dict) else None
        if not isinstance(info, dict) or not isinstance(urls, list):
            return []
        name = str(info.get("name") or package_name).strip()
        version = str(info.get("version") or "").strip()
        if not name or not version:
            return []

        source_url, digest = self._select_artifact(urls)
        if source_url is None:
            return []
        project_url = str(info.get("project_url") or "").strip()
        if not project_url.startswith(("https://", "http://")):
            project_url = f"https://pypi.org/project/{quote(name, safe='')}/{quote(version, safe='')}/"

        return [
            DiscoveryCandidate.create(
                provider=self.name,
                candidate_type="package",
                name=name,
                version=version,
                origin_uri=project_url,
                source_uri=source_url,
                pinned_version_or_commit=version,
                expected_digest=digest,
                capabilities=("package-source", "skill-source"),
                permissions=("network:http", "process:execute"),
                metadata={
                    "summary": info.get("summary"),
                    "license": info.get("license"),
                    "requires_python": info.get("requires_python"),
                },
            )
        ]

    @staticmethod
    def _select_artifact(urls: list) -> tuple[str | None, str | None]:
        ordered = sorted(
            (item for item in urls if isinstance(item, dict)),
            key=lambda item: 0 if item.get("packagetype") == "sdist" else 1,
        )
        for item in ordered:
            url = str(item.get("url") or "").strip()
            if not url.startswith(("https://", "http://")):
                continue
            digests = item.get("digests") if isinstance(item.get("digests"), dict) else {}
            digest = str(digests.get("sha256") or "").strip().lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                continue
            return url, digest
        return None, None
