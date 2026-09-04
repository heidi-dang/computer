"""Official-document discovery adapter over CPTR's existing web-search stack."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from cptr.services.factory_discovery import DiscoveryCandidate
from cptr.utils.web.search import web_search_handler

_URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


class OfficialDocsDiscoveryProvider:
    name = "official_docs"

    def __init__(
        self,
        *,
        searcher: Callable[[str], Awaitable[str]] = web_search_handler,
        allowed_domains: tuple[str, ...] = (),
    ) -> None:
        self._searcher = searcher
        self._allowed_domains = tuple(
            domain.strip().lower() for domain in allowed_domains if domain.strip()
        )

    async def discover(self, query: str, *, limit: int) -> list[DiscoveryCandidate]:
        raw = await self._searcher(query)
        if not isinstance(raw, str) or raw.startswith("Error:"):
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for match in _URL_RE.findall(raw):
            url = match.rstrip(".,;:!?")
            if url in seen or not self._allowed(url):
                continue
            seen.add(url)
            urls.append(url)
            if len(urls) >= max(1, int(limit)):
                break
        return [
            DiscoveryCandidate.create(
                provider=self.name,
                candidate_type="documentation",
                name=self._display_name(url),
                version=None,
                origin_uri=url,
                source_uri=url,
                pinned_version_or_commit=None,
                capabilities=("documentation", "web-research"),
                permissions=("network:http",),
                metadata={"search_query": query[:500]},
            )
            for url in urls
        ]

    def _allowed(self, url: str) -> bool:
        if not self._allowed_domains:
            return True
        hostname = (urlsplit(url).hostname or "").lower()
        return any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self._allowed_domains
        )

    @staticmethod
    def _display_name(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.hostname or 'documentation'}{parsed.path or '/'}"
