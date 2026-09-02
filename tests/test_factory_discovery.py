import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from cptr.services.factory_discovery import (
    DiscoveryBudget,
    DiscoveryBudgetExceeded,
    DiscoveryCandidate,
    FactoryDiscovery,
    QuarantineCache,
    SafeHttpArtifactFetcher,
    ResearchSignals,
    ResearchTriggerPolicy,
)
from cptr.services.factory_discovery_providers.github import GitHubDiscoveryProvider
from cptr.services.factory_discovery_providers.mcp_registry import McpRegistryDiscoveryProvider
from cptr.services.factory_discovery_providers.npm_registry import NpmRegistryDiscoveryProvider
from cptr.services.factory_discovery_providers.official_docs import OfficialDocsDiscoveryProvider
from cptr.services.factory_discovery_providers.pypi import PyPIRegistryDiscoveryProvider


class _FakeProvider:
    def __init__(self, name: str, candidates: list[DiscoveryCandidate], delay: float = 0.0):
        self.name = name
        self.candidates = candidates
        self.delay = delay
        self.calls = 0

    async def discover(self, query: str, *, limit: int) -> list[DiscoveryCandidate]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.candidates[:limit]


class _FakeFetcher:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = 0

    async def fetch(self, candidate: DiscoveryCandidate, *, max_bytes: int, timeout_ms: int) -> bytes:
        self.calls += 1
        if len(self.content) > max_bytes:
            raise DiscoveryBudgetExceeded("artifact exceeds byte budget")
        return self.content


def _candidate(name: str = "candidate", *, metadata: dict | None = None) -> DiscoveryCandidate:
    return DiscoveryCandidate.create(
        provider="fake",
        candidate_type="skill",
        name=name,
        version="1.0.0",
        origin_uri=f"https://example.invalid/{name}",
        source_uri=f"https://example.invalid/{name}.tar.gz",
        pinned_version_or_commit="a" * 40,
        capabilities=["repo-analysis"],
        permissions=["workspace:read"],
        metadata=metadata or {},
    )


class FactoryDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_research_trigger_policy_only_expands_for_persisted_signals(self):
        policy = ResearchTriggerPolicy(
            confidence_threshold=0.7,
            local_success_threshold=0.5,
            repeated_failure_threshold=2,
            disagreement_threshold=0.4,
        )

        quiet = policy.evaluate(ResearchSignals(confidence=0.95, local_success_rate=0.9))
        triggered = policy.evaluate(
            ResearchSignals(confidence=0.95, local_success_rate=0.9, unfamiliar_technology=True)
        )
        repeated = policy.evaluate(
            ResearchSignals(confidence=0.95, local_success_rate=0.9, repeated_failure_count=2)
        )

        self.assertFalse(quiet.enabled)
        self.assertEqual(quiet.reasons, ())
        self.assertTrue(triggered.enabled)
        self.assertEqual(triggered.reasons, ("unfamiliar_technology",))
        self.assertTrue(repeated.enabled)
        self.assertIn("repeated_failure", repeated.reasons)

    async def test_provider_and_result_limits_are_hard_bounds(self):
        first = _FakeProvider("first", [_candidate("a"), _candidate("b"), _candidate("c")])
        second = _FakeProvider("second", [_candidate("d")])
        discovery = FactoryDiscovery(providers=[first, second])
        budget = DiscoveryBudget(max_providers=1, max_results=2, max_bytes=100_000, max_runtime_ms=1000)

        batch = await discovery.discover(
            "repo analysis skill",
            signals=ResearchSignals(unfamiliar_technology=True),
            budget=budget,
        )

        self.assertTrue(batch.triggered)
        self.assertEqual([item.name for item in batch.candidates], ["a", "b"])
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 0)
        self.assertLessEqual(batch.bytes_used, budget.max_bytes)

    async def test_metadata_byte_budget_fails_closed(self):
        huge = _candidate("huge", metadata={"description": "x" * 4096})
        discovery = FactoryDiscovery(providers=[_FakeProvider("first", [huge])])

        with self.assertRaises(DiscoveryBudgetExceeded):
            await discovery.discover(
                "huge",
                signals=ResearchSignals(unfamiliar_technology=True),
                budget=DiscoveryBudget(
                    max_providers=1,
                    max_results=1,
                    max_bytes=256,
                    max_runtime_ms=1000,
                ),
            )

    async def test_runtime_budget_times_out_provider(self):
        slow = _FakeProvider("slow", [_candidate()], delay=0.05)
        discovery = FactoryDiscovery(providers=[slow])

        with self.assertRaises(DiscoveryBudgetExceeded):
            await discovery.discover(
                "slow",
                signals=ResearchSignals(unfamiliar_technology=True),
                budget=DiscoveryBudget(
                    max_providers=1,
                    max_results=1,
                    max_bytes=10_000,
                    max_runtime_ms=10,
                ),
            )

    async def test_fetched_executable_looking_content_is_only_quarantined(self):
        content = b"#!/bin/sh\ntouch SHOULD_NOT_EXIST\n"
        fetcher = _FakeFetcher(content)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "SHOULD_NOT_EXIST"
            cache = QuarantineCache(root / "quarantine")
            discovery = FactoryDiscovery(
                providers=[],
                artifact_fetcher=fetcher,
                quarantine_cache=cache,
            )

            with (
                patch("asyncio.create_subprocess_exec", side_effect=AssertionError("must not execute")),
                patch("subprocess.run", side_effect=AssertionError("must not execute")),
            ):
                artifact = await discovery.fetch_into_quarantine(
                    _candidate("shell-looking"),
                    budget=DiscoveryBudget(
                        max_providers=1,
                        max_results=1,
                        max_bytes=10_000,
                        max_runtime_ms=1000,
                    ),
                )

            self.assertEqual(fetcher.calls, 1)
            self.assertFalse(marker.exists())
            self.assertTrue(artifact.path.is_file())
            self.assertEqual(artifact.path.read_bytes(), content)
            self.assertEqual(artifact.size_bytes, len(content))
            self.assertEqual(artifact.path.stat().st_mode & 0o111, 0)
            self.assertEqual(os.stat(artifact.path).st_mode & 0o777, 0o600)
            self.assertEqual(artifact.read_bytes(max_bytes=100), content)


class DiscoveryProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_github_provider_returns_pinned_metadata_without_repository_execution(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if request.url.path == "/search/repositories":
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "full_name": "owner/repo",
                                "html_url": "https://github.com/owner/repo",
                                "default_branch": "main",
                                "description": "Useful skill",
                                "stargazers_count": 42,
                                "updated_at": "2026-09-01T00:00:00Z",
                            }
                        ]
                    },
                )
            if request.url.path == "/repos/owner/repo/commits/main":
                return httpx.Response(200, json={"sha": "b" * 40})
            raise AssertionError(f"unexpected request: {request.url}")

        provider = GitHubDiscoveryProvider(transport=httpx.MockTransport(handler))
        items = await provider.discover("repo analysis", limit=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].pinned_version_or_commit, "b" * 40)
        self.assertEqual(items[0].origin_uri, "https://github.com/owner/repo")
        self.assertEqual(
            items[0].source_uri,
            f"https://codeload.github.com/owner/repo/tar.gz/{'b' * 40}",
        )
        self.assertNotIn("token", str(items[0].to_dict()).lower())
        self.assertEqual(len(seen), 2)

    async def test_npm_provider_uses_exact_version_and_tarball_metadata(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/-/v1/search":
                return httpx.Response(
                    200,
                    json={
                        "objects": [
                            {
                                "package": {
                                    "name": "factory-skill",
                                    "version": "1.2.3",
                                    "description": "Factory skill",
                                    "links": {"npm": "https://www.npmjs.com/package/factory-skill"},
                                }
                            }
                        ]
                    },
                )
            if request.url.path == "/factory-skill/1.2.3":
                return httpx.Response(
                    200,
                    json={
                        "dist": {
                            "tarball": "https://registry.npmjs.org/factory-skill/-/factory-skill-1.2.3.tgz",
                            "integrity": "sha512-public-integrity-value",
                        }
                    },
                )
            raise AssertionError(f"unexpected request: {request.url}")

        provider = NpmRegistryDiscoveryProvider(transport=httpx.MockTransport(handler))
        items = await provider.discover("factory skill", limit=1)

        self.assertEqual(items[0].pinned_version_or_commit, "1.2.3")
        self.assertEqual(
            items[0].source_uri,
            "https://registry.npmjs.org/factory-skill/-/factory-skill-1.2.3.tgz",
        )
        self.assertEqual(items[0].metadata["registry_integrity"], "sha512-public-integrity-value")

    async def test_mcp_registry_provider_parses_official_server_list_shape(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/v0.1/servers")
            self.assertEqual(request.url.params["search"], "filesystem")
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {
                            "server": {
                                "name": "io.example/filesystem",
                                "version": "2.0.1",
                                "description": "Filesystem tools",
                                "repository": {"url": "https://github.com/example/filesystem"},
                                "packages": [
                                    {
                                        "registryType": "npm",
                                        "identifier": "@example/filesystem",
                                        "version": "2.0.1",
                                        "fileSha256": "c" * 64,
                                    }
                                ],
                            }
                        }
                    ],
                    "metadata": {"count": 1},
                },
            )

        provider = McpRegistryDiscoveryProvider(transport=httpx.MockTransport(handler))
        items = await provider.discover("filesystem", limit=1)

        self.assertEqual(items[0].name, "io.example/filesystem")
        self.assertEqual(items[0].pinned_version_or_commit, "2.0.1")
        self.assertIsNone(items[0].expected_digest)
        self.assertEqual(items[0].origin_uri, "https://github.com/example/filesystem")

    async def test_mcp_registry_keeps_digest_only_when_bound_to_same_artifact_url(self):
        artifact_url = "https://downloads.example.com/filesystem-2.0.1.mcpb"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {
                            "server": {
                                "name": "io.example/filesystem",
                                "version": "2.0.1",
                                "repository": {"url": "https://github.com/example/filesystem"},
                                "packages": [
                                    {
                                        "registryType": "remote",
                                        "identifier": artifact_url,
                                        "version": "2.0.1",
                                        "fileSha256": "f" * 64,
                                    }
                                ],
                            }
                        }
                    ]
                },
            )

        provider = McpRegistryDiscoveryProvider(transport=httpx.MockTransport(handler))
        items = await provider.discover("filesystem", limit=1)

        self.assertEqual(items[0].source_uri, artifact_url)
        self.assertEqual(items[0].expected_digest, "f" * 64)

    async def test_safe_http_artifact_fetcher_requires_public_https_and_bounds_stream(self):
        payload = b"safe artifact bytes"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), "https://downloads.example.com/artifact.bin")
            return httpx.Response(200, content=payload)

        candidate = DiscoveryCandidate.create(
            provider="test",
            candidate_type="package",
            name="artifact",
            version="1.0.0",
            origin_uri="https://downloads.example.com/artifact",
            source_uri="https://downloads.example.com/artifact.bin",
            pinned_version_or_commit="1.0.0",
        )
        fetcher = SafeHttpArtifactFetcher(
            allowed_hosts=("downloads.example.com",),
            transport=httpx.MockTransport(handler),
        )

        self.assertEqual(
            await fetcher.fetch(candidate, max_bytes=len(payload), timeout_ms=1000),
            payload,
        )
        with self.assertRaises(DiscoveryBudgetExceeded):
            await fetcher.fetch(candidate, max_bytes=len(payload) - 1, timeout_ms=1000)

        for unsafe in (
            "http://downloads.example.com/artifact.bin",
            "https://127.0.0.1/artifact.bin",
            "https://localhost/artifact.bin",
        ):
            unsafe_candidate = DiscoveryCandidate.create(
                provider="test",
                candidate_type="package",
                name="unsafe",
                version="1.0.0",
                origin_uri="https://example.com/unsafe",
                source_uri=unsafe,
                pinned_version_or_commit="1.0.0",
            )
            with self.assertRaises(ValueError):
                await fetcher.fetch(unsafe_candidate, max_bytes=1024, timeout_ms=1000)

    async def test_safe_http_artifact_fetcher_refuses_unconfigured_hosts_by_default(self):
        fetcher = SafeHttpArtifactFetcher(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=b"x"))
        )
        with self.assertRaises(ValueError):
            await fetcher.fetch(_candidate(), max_bytes=1024, timeout_ms=1000)

    async def test_safe_http_artifact_fetcher_refuses_redirects(self):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/artifact"})

        fetcher = SafeHttpArtifactFetcher(
            allowed_hosts=("example.invalid",),
            transport=httpx.MockTransport(handler),
        )
        with self.assertRaises(httpx.HTTPStatusError):
            await fetcher.fetch(_candidate(), max_bytes=1024, timeout_ms=1000)

    async def test_pypi_provider_returns_exact_sdist_version_and_digest(self):
        source_url = "https://files.pythonhosted.org/packages/factory-skill-3.4.5.tar.gz"

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/pypi/factory-skill/json")
            return httpx.Response(
                200,
                json={
                    "info": {
                        "name": "factory-skill",
                        "version": "3.4.5",
                        "summary": "Factory skill",
                        "project_url": "https://pypi.org/project/factory-skill/",
                    },
                    "urls": [
                        {
                            "packagetype": "sdist",
                            "url": source_url,
                            "digests": {"sha256": "e" * 64},
                        }
                    ],
                },
            )

        provider = PyPIRegistryDiscoveryProvider(transport=httpx.MockTransport(handler))
        items = await provider.discover("factory-skill", limit=1)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].pinned_version_or_commit, "3.4.5")
        self.assertEqual(items[0].source_uri, source_url)
        self.assertEqual(items[0].expected_digest, "e" * 64)
        self.assertEqual(await provider.discover("broad search terms", limit=5), [])

    async def test_official_docs_provider_reuses_existing_web_search_and_filters_domains(self):
        calls: list[str] = []

        async def searcher(query: str) -> str:
            calls.append(query)
            return (
                "Official: https://docs.example.com/api/v2\n"
                "Community: https://untrusted.example.net/post\n"
                "Duplicate: https://docs.example.com/api/v2"
            )

        provider = OfficialDocsDiscoveryProvider(
            searcher=searcher,
            allowed_domains=("docs.example.com",),
        )
        items = await provider.discover("api v2", limit=5)

        self.assertEqual(calls, ["api v2"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].origin_uri, "https://docs.example.com/api/v2")
        self.assertEqual(items[0].candidate_type, "documentation")


if __name__ == "__main__":
    unittest.main()
