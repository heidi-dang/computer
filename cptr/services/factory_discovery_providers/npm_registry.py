"""npm registry metadata discovery for Dark Factory capability research."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from cptr.services.factory_discovery import DiscoveryCandidate


class NpmRegistryDiscoveryProvider:
    name = "npm_registry"

    def __init__(
        self,
        *,
        registry_base_url: str = "https://registry.npmjs.org",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._registry_base_url = registry_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def discover(self, query: str, *, limit: int) -> list[DiscoveryCandidate]:
        limit = max(1, min(int(limit), 250))
        async with httpx.AsyncClient(
            base_url=self._registry_base_url,
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
            headers={"User-Agent": "cptr-dark-factory"},
        ) as client:
            response = await client.get("/-/v1/search", params={"text": query, "size": limit})
            response.raise_for_status()
            payload = response.json()
            objects = payload.get("objects") if isinstance(payload, dict) else None
            if not isinstance(objects, list):
                return []

            candidates: list[DiscoveryCandidate] = []
            for entry in objects[:limit]:
                if not isinstance(entry, dict) or not isinstance(entry.get("package"), dict):
                    continue
                package = entry["package"]
                name = str(package.get("name") or "").strip()
                version = str(package.get("version") or "").strip()
                if not name or not version:
                    continue
                metadata = await self._version_metadata(client, name, version)
                dist = metadata.get("dist") if isinstance(metadata, dict) else None
                dist = dist if isinstance(dist, dict) else {}
                tarball = str(dist.get("tarball") or "").strip() or None
                links = package.get("links") if isinstance(package.get("links"), dict) else {}
                origin_uri = str(links.get("npm") or "").strip()
                if not origin_uri:
                    origin_uri = f"https://www.npmjs.com/package/{quote(name, safe='@/')}"
                candidates.append(
                    DiscoveryCandidate.create(
                        provider=self.name,
                        candidate_type="package",
                        name=name,
                        version=version,
                        origin_uri=origin_uri,
                        source_uri=tarball or origin_uri,
                        pinned_version_or_commit=version,
                        capabilities=("package-source", "skill-source"),
                        permissions=("network:http", "process:execute"),
                        metadata={
                            "description": package.get("description"),
                            "updated_at": package.get("date"),
                            "registry_integrity": dist.get("integrity"),
                            "registry_shasum": dist.get("shasum"),
                            "publisher": (
                                package.get("publisher", {}).get("username")
                                if isinstance(package.get("publisher"), dict)
                                else None
                            ),
                        },
                    )
                )
            return candidates

    async def _version_metadata(
        self,
        client: httpx.AsyncClient,
        name: str,
        version: str,
    ) -> dict:
        try:
            path = f"/{quote(name, safe='')}/{quote(version, safe='')}"
            response = await client.get(path)
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}
        except (httpx.HTTPError, ValueError, TypeError):
            return {}
