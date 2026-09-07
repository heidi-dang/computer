"""Official/configurable MCP registry metadata discovery."""

from __future__ import annotations

from urllib.parse import quote

import httpx

from cptr.services.factory_discovery import DiscoveryCandidate


class McpRegistryDiscoveryProvider:
    name = "mcp_registry"

    def __init__(
        self,
        *,
        registry_base_url: str = "https://registry.modelcontextprotocol.io",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._registry_base_url = registry_base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport

    async def discover(self, query: str, *, limit: int) -> list[DiscoveryCandidate]:
        limit = max(1, min(int(limit), 100))
        async with httpx.AsyncClient(
            base_url=self._registry_base_url,
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
            headers={"User-Agent": "cptr-dark-factory", "Accept": "application/json"},
        ) as client:
            response = await client.get(
                "/v0.1/servers",
                params={"search": query, "limit": limit, "version": "latest"},
            )
            response.raise_for_status()
            payload = response.json()
            raw_servers = payload.get("servers") if isinstance(payload, dict) else None
            if not isinstance(raw_servers, list):
                return []

            candidates: list[DiscoveryCandidate] = []
            for entry in raw_servers[:limit]:
                if not isinstance(entry, dict):
                    continue
                raw = entry.get("server") if isinstance(entry.get("server"), dict) else entry
                name = str(raw.get("name") or "").strip()
                version = str(raw.get("version") or "").strip()
                if not name:
                    continue
                repository = raw.get("repository")
                repository_url = ""
                if isinstance(repository, dict):
                    repository_url = str(repository.get("url") or "").strip()
                elif isinstance(repository, str):
                    repository_url = repository.strip()
                fallback_origin = (
                    f"{self._registry_base_url}/v0.1/servers/"
                    f"{quote(name, safe='')}/versions/{quote(version or 'latest', safe='')}"
                )
                origin_uri = repository_url or fallback_origin
                packages = raw.get("packages")
                if not isinstance(packages, list):
                    packages = (
                        entry.get("packages") if isinstance(entry.get("packages"), list) else []
                    )
                remotes = raw.get("remotes")
                if not isinstance(remotes, list):
                    remotes = entry.get("remotes") if isinstance(entry.get("remotes"), list) else []
                safe_remotes = [item for item in (self._safe_remote(remote) for remote in remotes[:20]) if item]
                package_source, expected_digest = self._artifact_source(packages)
                permissions = ["network:http"]
                if packages:
                    permissions.append("process:execute")
                capabilities = ["mcp-server", "tool-provider"]
                if safe_remotes:
                    capabilities.append("remote-mcp")
                if packages:
                    capabilities.append("packaged-mcp")
                candidates.append(
                    DiscoveryCandidate.create(
                        provider=self.name,
                        candidate_type="mcp_server",
                        name=name,
                        version=version or None,
                        origin_uri=origin_uri,
                        source_uri=package_source,
                        pinned_version_or_commit=(
                            version if version and version.lower() != "latest" else None
                        ),
                        expected_digest=expected_digest,
                        capabilities=capabilities,
                        permissions=permissions,
                        metadata={
                            "description": raw.get("description"),
                            "status": raw.get("status"),
                            "packages": [self._safe_package(package) for package in packages[:20]],
                            "remotes": safe_remotes,
                        },
                    )
                )
            return candidates

    @staticmethod
    def _artifact_source(packages: list) -> tuple[str | None, str | None]:
        """Return a source URL and only the SHA-256 published for that same item."""
        for package in packages:
            if not isinstance(package, dict):
                continue
            identifier = str(package.get("identifier") or "").strip()
            if not identifier.startswith(("https://", "http://")):
                continue
            digest = str(package.get("fileSha256") or "").strip().lower()
            verified_digest = (
                digest
                if len(digest) == 64 and all(char in "0123456789abcdef" for char in digest)
                else None
            )
            return identifier, verified_digest
        return None, None

    @staticmethod
    def _safe_remote(remote: object) -> dict:
        if not isinstance(remote, dict):
            return {}
        url = str(remote.get("url") or "").strip()
        transport_type = str(remote.get("type") or "").strip().lower()
        if not url.startswith("https://") or transport_type != "streamable-http":
            return {}
        return {"url": url, "type": transport_type}

    @staticmethod
    def _safe_package(package: object) -> dict:
        if not isinstance(package, dict):
            return {}
        return {
            key: package.get(key)
            for key in ("registryType", "identifier", "version", "transport", "fileSha256")
            if package.get(key) is not None
        }
