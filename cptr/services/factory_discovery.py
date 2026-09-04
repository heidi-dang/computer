"""Bounded external capability discovery and non-executable quarantine storage.

External content is data in this layer. Discovery providers return metadata only;
artifact fetchers return bytes only; quarantine storage writes those bytes without
execution permissions. Trust promotion happens separately in ``factory_trust``.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import ipaddress
import json
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx

from cptr.env import DATA_DIR


class DiscoveryBudgetExceeded(RuntimeError):
    """Raised when a discovery or fetch operation exceeds an explicit budget."""


@dataclass(frozen=True)
class DiscoveryBudget:
    max_providers: int
    max_results: int
    max_bytes: int
    max_runtime_ms: int

    def __post_init__(self) -> None:
        for name in ("max_providers", "max_results", "max_bytes", "max_runtime_ms"):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class ResearchSignals:
    confidence: float | None = None
    unfamiliar_technology: bool = False
    repeated_failure_count: int = 0
    local_success_rate: float | None = None
    security_sensitive: bool = False
    api_uncertain: bool = False
    performance_regression: bool = False
    disagreement_score: float = 0.0
    verifier_contradiction: bool = False


@dataclass(frozen=True)
class ResearchTriggerDecision:
    enabled: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResearchTriggerPolicy:
    confidence_threshold: float = 0.65
    local_success_threshold: float = 0.55
    repeated_failure_threshold: int = 2
    disagreement_threshold: float = 0.5

    def __post_init__(self) -> None:
        for name in ("confidence_threshold", "local_success_threshold", "disagreement_threshold"):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.repeated_failure_threshold < 1:
            raise ValueError("repeated_failure_threshold must be at least 1")

    def evaluate(self, signals: ResearchSignals) -> ResearchTriggerDecision:
        reasons: list[str] = []
        if signals.confidence is not None and signals.confidence < self.confidence_threshold:
            reasons.append("low_confidence")
        if signals.unfamiliar_technology:
            reasons.append("unfamiliar_technology")
        if signals.repeated_failure_count >= self.repeated_failure_threshold:
            reasons.append("repeated_failure")
        if (
            signals.local_success_rate is not None
            and signals.local_success_rate < self.local_success_threshold
        ):
            reasons.append("local_capability_underperforming")
        if signals.security_sensitive:
            reasons.append("security_sensitive")
        if signals.api_uncertain:
            reasons.append("api_uncertainty")
        if signals.performance_regression:
            reasons.append("performance_regression")
        if signals.disagreement_score >= self.disagreement_threshold:
            reasons.append("role_disagreement")
        if signals.verifier_contradiction:
            reasons.append("verifier_contradiction")
        return ResearchTriggerDecision(enabled=bool(reasons), reasons=tuple(reasons))


def _normalized_tokens(values: Iterable[str]) -> tuple[str, ...]:
    normalized = {str(value).strip().lower() for value in values if str(value).strip()}
    return tuple(sorted(normalized))


def _safe_uri(raw: str) -> str:
    value = str(raw or "").strip()
    if not value:
        raise ValueError("URI must not be blank")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("external discovery URI must be http(s)")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (parsed.scheme == "https" and parsed.port == 443) or (
        parsed.scheme == "http" and parsed.port == 80
    )
    netloc = host if parsed.port is None or default_port else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "", "", ""))


def _safe_metadata(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:8192]
    if isinstance(value, dict):
        blocked = {
            "authorization",
            "cookie",
            "credential",
            "env",
            "header",
            "password",
            "secret",
            "token",
        }
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            key_text = str(key)[:160]
            if any(fragment in key_text.lower() for fragment in blocked):
                continue
            result[key_text] = _safe_metadata(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item, depth=depth + 1) for item in list(value)[:100]]
    return str(value)[:1024]


@dataclass(frozen=True)
class DiscoveryCandidate:
    stable_id: str
    provider: str
    candidate_type: str
    name: str
    version: str | None
    origin_uri: str
    source_uri: str | None
    pinned_version_or_commit: str | None
    expected_digest: str | None
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        provider: str,
        candidate_type: str,
        name: str,
        version: str | None,
        origin_uri: str,
        source_uri: str | None = None,
        pinned_version_or_commit: str | None = None,
        expected_digest: str | None = None,
        capabilities: Iterable[str] = (),
        permissions: Iterable[str] = (),
        metadata: dict[str, Any] | None = None,
    ) -> "DiscoveryCandidate":
        normalized_provider = provider.strip().lower()
        normalized_type = candidate_type.strip().lower()
        normalized_name = name.strip()
        if not normalized_provider or not normalized_type or not normalized_name:
            raise ValueError("provider, candidate_type, and name must not be blank")
        digest = expected_digest.strip().lower() if expected_digest else None
        if digest is not None and (
            len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ValueError("expected_digest must be a SHA-256 hex digest")
        logical = f"{normalized_provider}\0{normalized_type}\0{normalized_name.lower()}"
        stable_digest = hashlib.sha256(logical.encode("utf-8")).hexdigest()[:24]
        return cls(
            stable_id=f"discover_{stable_digest}",
            provider=normalized_provider,
            candidate_type=normalized_type,
            name=normalized_name,
            version=str(version).strip() if version is not None and str(version).strip() else None,
            origin_uri=_safe_uri(origin_uri),
            source_uri=_safe_uri(source_uri) if source_uri else None,
            pinned_version_or_commit=(
                str(pinned_version_or_commit).strip() if pinned_version_or_commit else None
            ),
            expected_digest=digest,
            capabilities=_normalized_tokens(capabilities),
            permissions=_normalized_tokens(permissions),
            metadata=_safe_metadata(metadata or {}),
        )

    @property
    def identity(self) -> str:
        pin = self.pinned_version_or_commit or self.version or "unversioned"
        return f"{self.stable_id}:{pin}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "provider": self.provider,
            "candidate_type": self.candidate_type,
            "name": self.name,
            "version": self.version,
            "origin_uri": self.origin_uri,
            "source_uri": self.source_uri,
            "pinned_version_or_commit": self.pinned_version_or_commit,
            "expected_digest": self.expected_digest,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "metadata": _safe_metadata(self.metadata),
        }


class DiscoveryProvider(Protocol):
    name: str

    async def discover(self, query: str, *, limit: int) -> list[DiscoveryCandidate]: ...


class ArtifactFetcher(Protocol):
    async def fetch(
        self,
        candidate: DiscoveryCandidate,
        *,
        max_bytes: int,
        timeout_ms: int,
    ) -> bytes: ...


_DEFAULT_ARTIFACT_HOSTS = (
    "codeload.github.com",
    "files.pythonhosted.org",
    "pypi.org",
    "registry.modelcontextprotocol.io",
    "registry.npmjs.org",
)


class SafeHttpArtifactFetcher:
    """Fetch bounded public HTTPS bytes without following redirects.

    Host validation rejects local/private literal addresses and, for the real
    network transport, resolves DNS before the request and rejects any
    non-public address. A custom httpx transport is treated as an explicit test
    seam and therefore skips DNS resolution while retaining literal-host and
    scheme validation.
    """

    def __init__(
        self,
        *,
        allowed_hosts: Iterable[str] = (),
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        configured_hosts = tuple(allowed_hosts) or _DEFAULT_ARTIFACT_HOSTS
        self._allowed_hosts = tuple(
            sorted({host.strip().lower().rstrip(".") for host in configured_hosts if host.strip()})
        )
        self._timeout_seconds = float(timeout_seconds)
        self._transport = transport

    @staticmethod
    def _is_public_ip(value: str) -> bool:
        address = ipaddress.ip_address(value)
        return not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        )

    def _host_allowed(self, host: str) -> bool:
        return any(
            host == allowed or host.endswith(f".{allowed}") for allowed in self._allowed_hosts
        )

    async def _validate_source(self, raw_url: str) -> str:
        parsed = urlsplit(raw_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("artifact source must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("artifact source must not contain userinfo")
        host = parsed.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost"):
            raise ValueError("artifact source must not target localhost")
        if not self._host_allowed(host):
            raise ValueError("artifact source host is not allowed")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not self._is_public_ip(str(literal)):
            raise ValueError("artifact source must target a public address")

        if self._transport is None and literal is None:
            port = parsed.port or 443
            try:
                addresses = await asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    port,
                    0,
                    socket.SOCK_STREAM,
                )
            except OSError as exc:
                raise ValueError("artifact source host could not be resolved") from exc
            resolved = {str(entry[4][0]) for entry in addresses if entry and entry[4]}
            if not resolved or any(not self._is_public_ip(address) for address in resolved):
                raise ValueError("artifact source DNS resolved to a non-public address")
        return raw_url

    async def fetch(
        self,
        candidate: DiscoveryCandidate,
        *,
        max_bytes: int,
        timeout_ms: int,
    ) -> bytes:
        if max_bytes <= 0 or timeout_ms <= 0:
            raise ValueError("artifact fetch bounds must be positive")
        if not candidate.source_uri:
            raise ValueError("candidate has no artifact source URI")
        url = await self._validate_source(candidate.source_uri)
        timeout = min(self._timeout_seconds, timeout_ms / 1000)
        chunks: list[bytes] = []
        total = 0
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,
            transport=self._transport,
            headers={"User-Agent": "cptr-dark-factory", "Accept": "*/*"},
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > max_bytes:
                            raise DiscoveryBudgetExceeded("artifact exceeds byte budget")
                    except ValueError:
                        pass
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise DiscoveryBudgetExceeded("artifact exceeds byte budget")
                    chunks.append(chunk)
        return b"".join(chunks)


@dataclass(frozen=True)
class DiscoveryBatch:
    triggered: bool
    trigger_reasons: tuple[str, ...]
    candidates: tuple[DiscoveryCandidate, ...]
    providers_considered: tuple[str, ...]
    provider_errors: tuple[str, ...]
    bytes_used: int
    runtime_ms: int


@dataclass(frozen=True)
class QuarantinedArtifact:
    stable_id: str
    candidate_identity: str
    digest: str
    path: Path
    size_bytes: int
    stored_at_ms: int

    def read_bytes(self, *, max_bytes: int) -> bytes:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        stat = self.path.stat()
        if stat.st_size > max_bytes:
            raise ValueError("quarantined artifact exceeds read bound")
        data = self.path.read_bytes()
        if hashlib.sha256(data).hexdigest() != self.digest:
            raise ValueError("quarantined artifact digest changed")
        return data


class QuarantineCache:
    """Digest-addressed, non-executable storage for fetched external bytes."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else DATA_DIR / "factory-quarantine"

    def store(self, candidate: DiscoveryCandidate, content: bytes) -> QuarantinedArtifact:
        if not isinstance(content, bytes):
            raise TypeError("quarantine content must be bytes")
        digest = hashlib.sha256(content).hexdigest()
        stored_at_ms = int(time.time() * 1000)
        candidate_dir = self.root / candidate.stable_id
        candidate_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(candidate_dir, 0o700)
        except OSError:
            pass

        blob = candidate_dir / f"{digest}.blob"
        if not blob.exists():
            temp = candidate_dir / f".{digest}.{uuid.uuid4().hex}.tmp"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, blob)
            finally:
                if temp.exists():
                    temp.unlink(missing_ok=True)
        os.chmod(blob, 0o600)

        metadata = {
            "candidate": candidate.to_dict(),
            "digest": digest,
            "size_bytes": len(content),
            "stored_at_ms": stored_at_ms,
            "executable": False,
        }
        meta_path = candidate_dir / f"{digest}.json"
        raw = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temp_meta = candidate_dir / f".{digest}.{uuid.uuid4().hex}.json.tmp"
        fd = os.open(temp_meta, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_meta, meta_path)
        finally:
            if temp_meta.exists():
                temp_meta.unlink(missing_ok=True)
        os.chmod(meta_path, 0o600)

        return QuarantinedArtifact(
            stable_id=candidate.stable_id,
            candidate_identity=candidate.identity,
            digest=digest,
            path=blob,
            size_bytes=len(content),
            stored_at_ms=stored_at_ms,
        )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class FactoryDiscovery:
    def __init__(
        self,
        *,
        providers: Iterable[DiscoveryProvider],
        trigger_policy: ResearchTriggerPolicy | None = None,
        artifact_fetcher: ArtifactFetcher | None = None,
        quarantine_cache: QuarantineCache | None = None,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._providers = tuple(providers)
        self._trigger_policy = trigger_policy or ResearchTriggerPolicy()
        self._artifact_fetcher = artifact_fetcher
        self._quarantine_cache = quarantine_cache
        self._monotonic = monotonic

    def _remaining_ms(self, start: float, budget: DiscoveryBudget) -> int:
        elapsed_ms = int((self._monotonic() - start) * 1000)
        remaining = budget.max_runtime_ms - elapsed_ms
        if remaining <= 0:
            raise DiscoveryBudgetExceeded("runtime budget exceeded")
        return remaining

    async def discover(
        self,
        query: str,
        *,
        signals: ResearchSignals,
        budget: DiscoveryBudget,
    ) -> DiscoveryBatch:
        normalized_query = query.strip()
        if not normalized_query:
            raise ValueError("discovery query must not be blank")
        decision = self._trigger_policy.evaluate(signals)
        if not decision.enabled:
            return DiscoveryBatch(
                triggered=False,
                trigger_reasons=decision.reasons,
                candidates=(),
                providers_considered=(),
                provider_errors=(),
                bytes_used=0,
                runtime_ms=0,
            )

        start = self._monotonic()
        candidates: list[DiscoveryCandidate] = []
        providers_considered: list[str] = []
        provider_errors: list[str] = []
        bytes_used = 0

        for provider in self._providers[: budget.max_providers]:
            if len(candidates) >= budget.max_results:
                break
            provider_name = str(getattr(provider, "name", provider.__class__.__name__))[:120]
            providers_considered.append(provider_name)
            remaining_ms = self._remaining_ms(start, budget)
            try:
                result = await asyncio.wait_for(
                    _maybe_await(
                        provider.discover(
                            normalized_query,
                            limit=budget.max_results - len(candidates),
                        )
                    ),
                    timeout=remaining_ms / 1000,
                )
            except TimeoutError as exc:
                raise DiscoveryBudgetExceeded("runtime budget exceeded") from exc
            except DiscoveryBudgetExceeded:
                raise
            except Exception as exc:
                provider_errors.append(f"{provider_name}:{exc.__class__.__name__}")
                continue

            if not isinstance(result, list):
                provider_errors.append(f"{provider_name}:invalid_result")
                continue
            for candidate in result:
                if not isinstance(candidate, DiscoveryCandidate):
                    provider_errors.append(f"{provider_name}:invalid_candidate")
                    continue
                raw = json.dumps(candidate.to_dict(), sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
                if bytes_used + len(raw) > budget.max_bytes:
                    raise DiscoveryBudgetExceeded("metadata byte budget exceeded")
                bytes_used += len(raw)
                candidates.append(candidate)
                if len(candidates) >= budget.max_results:
                    break
            self._remaining_ms(start, budget)

        runtime_ms = int((self._monotonic() - start) * 1000)
        return DiscoveryBatch(
            triggered=True,
            trigger_reasons=decision.reasons,
            candidates=tuple(candidates),
            providers_considered=tuple(providers_considered),
            provider_errors=tuple(provider_errors),
            bytes_used=bytes_used,
            runtime_ms=runtime_ms,
        )

    async def fetch_into_quarantine(
        self,
        candidate: DiscoveryCandidate,
        *,
        budget: DiscoveryBudget,
    ) -> QuarantinedArtifact:
        if self._artifact_fetcher is None or self._quarantine_cache is None:
            raise RuntimeError("artifact fetcher and quarantine cache must be configured")
        start = self._monotonic()
        try:
            content = await asyncio.wait_for(
                _maybe_await(
                    self._artifact_fetcher.fetch(
                        candidate,
                        max_bytes=budget.max_bytes,
                        timeout_ms=budget.max_runtime_ms,
                    )
                ),
                timeout=budget.max_runtime_ms / 1000,
            )
        except TimeoutError as exc:
            raise DiscoveryBudgetExceeded("artifact fetch runtime budget exceeded") from exc
        if not isinstance(content, bytes):
            raise TypeError("artifact fetcher must return bytes")
        if len(content) > budget.max_bytes:
            raise DiscoveryBudgetExceeded("artifact exceeds byte budget")
        if int((self._monotonic() - start) * 1000) > budget.max_runtime_ms:
            raise DiscoveryBudgetExceeded("artifact fetch runtime budget exceeded")
        return self._quarantine_cache.store(candidate, content)
