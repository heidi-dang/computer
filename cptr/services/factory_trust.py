"""Fail-closed trust evaluation for quarantined external capabilities.

This module never executes candidate content. It validates immutable identity,
re-hashes quarantined bytes, compares permissions, performs bounded static,
dependency/supply-chain and prompt-injection audits, and only then may invoke an
explicit constrained capability-test adapter. Approved-cache entries contain
only structured trust metadata and are revalidated against pin, digest, policy,
and TTL before reuse.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import io
import json
import os
import re
import stat
import tarfile
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit

from cptr.env import DATA_DIR
from cptr.services.factory_capabilities import (
    CapabilityManifest,
    CapabilityTrustStatus,
    CapabilityVerificationStatus,
)
from cptr.services.factory_discovery import DiscoveryCandidate, QuarantinedArtifact

_MUTABLE_PINS = {
    "head",
    "latest",
    "main",
    "master",
    "nightly",
    "trunk",
    "develop",
    "development",
}
_INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions?\b", re.IGNORECASE),
    re.compile(
        r"\boverride\s+(?:the\s+)?(?:system|factory|security)\s+(?:prompt|policy|rules?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\breveal\s+(?:the\s+)?(?:system\s+prompt|secrets?|credentials?|tokens?)\b", re.IGNORECASE
    ),
    re.compile(
        r"\bdisable\s+(?:the\s+)?(?:security|safety|approval|verification)\b", re.IGNORECASE
    ),
    re.compile(r"\bdo\s+not\s+obey\s+(?:the\s+)?(?:system|developer|user|policy)\b", re.IGNORECASE),
)
_UNSAFE_INSTALL_PATTERNS = (
    re.compile(r"\b(?:curl|wget)\b[^\n|]{0,500}\|\s*(?:sh|bash|zsh)\b", re.IGNORECASE),
    re.compile(
        r"\bpip(?:3)?\s+install\b[^\n]*\bgit\+https?://[^\s]+@(?:main|master|head|latest)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnpm\s+(?:install|i)\b[^\n]*\b(?:https?://|git\+|github:)", re.IGNORECASE),
    re.compile(
        r"\b(?:npm|pnpm|yarn)\b[^\n]*\b(?:--ignore-scripts=false|--unsafe-perm)\b", re.IGNORECASE
    ),
)
_DANGEROUS_STATIC_PATTERNS = (
    re.compile(r"\brm\s+-rf\s+/(?:\s|$)", re.IGNORECASE),
    re.compile(r"\bsudo\s+", re.IGNORECASE),
    re.compile(r"\bchmod\s+\+x\b", re.IGNORECASE),
    re.compile(r"(?:~|/home/[^/]+)/(?:\.ssh|\.aws|\.config/gcloud)\b", re.IGNORECASE),
)
_EXACT_SEMVER = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4096]
    if isinstance(value, dict):
        blocked = {"authorization", "cookie", "credential", "password", "secret", "token"}
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:100]:
            key_text = str(key)[:160]
            if any(fragment in key_text.lower() for fragment in blocked):
                continue
            result[key_text] = _safe_json(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_json(item, depth=depth + 1) for item in list(value)[:100]]
    return str(value)[:1024]


@dataclass(frozen=True)
class TrustFinding:
    category: str
    code: str
    message: str
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "code": self.code,
            "message": self.message[:500],
            "blocking": self.blocking,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrustFinding":
        return cls(
            category=str(value.get("category") or "unknown")[:80],
            code=str(value.get("code") or "unknown")[:120],
            message=str(value.get("message") or "")[:500],
            blocking=bool(value.get("blocking", True)),
        )


@dataclass(frozen=True)
class CapabilityTestResult:
    passed: bool
    evidence_id: str
    runtime_ms: int
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id.strip():
            raise ValueError("capability test evidence_id must not be blank")
        if self.runtime_ms < 0:
            raise ValueError("capability test runtime_ms must be non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "evidence_id": self.evidence_id[:200],
            "runtime_ms": self.runtime_ms,
            "details": _safe_json(self.details),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityTestResult":
        return cls(
            passed=bool(value.get("passed")),
            evidence_id=str(value.get("evidence_id") or "cached-capability-test"),
            runtime_ms=max(0, int(value.get("runtime_ms") or 0)),
            details=_safe_json(value.get("details") or {}),
        )


@dataclass(frozen=True)
class CapabilityTestRequest:
    """Data-only request handed to a trusted constrained test runner."""

    stable_id: str
    candidate_identity: str
    artifact_digest: str
    artifact_bytes: bytes
    permissions: tuple[str, ...]
    artifact_writable: bool
    host_workspace_access: bool
    network_allowed: bool
    max_runtime_ms: int


@dataclass(frozen=True)
class TrustPolicy:
    allowed_permissions: tuple[str, ...]
    allow_network: bool
    require_pinned_external: bool = True
    require_capability_test: bool = True
    cache_ttl_ms: int = 24 * 60 * 60 * 1000
    max_artifact_bytes: int = 2 * 1024 * 1024
    capability_test_timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        normalized = tuple(
            sorted({item.strip().lower() for item in self.allowed_permissions if item.strip()})
        )
        object.__setattr__(self, "allowed_permissions", normalized)
        for name in ("cache_ttl_ms", "max_artifact_bytes", "capability_test_timeout_ms"):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")

    @property
    def fingerprint(self) -> str:
        payload = {
            "allowed_permissions": self.allowed_permissions,
            "allow_network": self.allow_network,
            "require_pinned_external": self.require_pinned_external,
            "require_capability_test": self.require_capability_test,
            "cache_ttl_ms": self.cache_ttl_ms,
            "max_artifact_bytes": self.max_artifact_bytes,
            "capability_test_timeout_ms": self.capability_test_timeout_ms,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class TrustCandidate:
    discovery: DiscoveryCandidate
    artifact: QuarantinedArtifact
    manifest: CapabilityManifest


class CapabilityTestAdapter(Protocol):
    async def test(
        self,
        candidate: TrustCandidate,
        policy: TrustPolicy,
    ) -> CapabilityTestResult: ...


class ConstrainedCapabilityTestAdapter:
    """Bridge a quarantined artifact into an explicitly constrained runner.

    The runner receives immutable bytes plus deny-by-default capability flags.
    This adapter never exposes a host workspace path, never marks the artifact
    writable, and never grants network access, even when the broader mission
    policy permits networking for other phases.
    """

    def __init__(self, *, runner: Any, max_artifact_bytes: int) -> None:
        if not callable(runner):
            raise TypeError("capability test runner must be callable")
        if max_artifact_bytes <= 0:
            raise ValueError("max_artifact_bytes must be positive")
        self._runner = runner
        self._max_artifact_bytes = int(max_artifact_bytes)

    async def _invoke(self, request: CapabilityTestRequest) -> Any:
        if inspect.iscoroutinefunction(self._runner):
            return await self._runner(request)
        result = await asyncio.to_thread(self._runner, request)
        if inspect.isawaitable(result):
            return await result
        return result

    async def test(
        self,
        candidate: TrustCandidate,
        policy: TrustPolicy,
    ) -> CapabilityTestResult:
        bound = min(self._max_artifact_bytes, policy.max_artifact_bytes)
        artifact_bytes = candidate.artifact.read_bytes(max_bytes=bound)
        request = CapabilityTestRequest(
            stable_id=candidate.discovery.stable_id,
            candidate_identity=candidate.discovery.identity,
            artifact_digest=candidate.artifact.digest,
            artifact_bytes=artifact_bytes,
            permissions=tuple(sorted(candidate.manifest.permissions)),
            artifact_writable=False,
            host_workspace_access=False,
            network_allowed=False,
            max_runtime_ms=policy.capability_test_timeout_ms,
        )
        result = await asyncio.wait_for(
            self._invoke(request),
            timeout=policy.capability_test_timeout_ms / 1000,
        )
        if not isinstance(result, CapabilityTestResult):
            raise TypeError("constrained capability test runner returned an invalid result")
        return result


@dataclass(frozen=True)
class TrustEvaluation:
    stable_id: str
    candidate_identity: str
    pin: str | None
    digest: str
    provenance: dict[str, Any]
    permissions: tuple[str, ...]
    static_findings: tuple[TrustFinding, ...]
    dependency_findings: tuple[TrustFinding, ...]
    injection_findings: tuple[TrustFinding, ...]
    capability_test: CapabilityTestResult | None
    final_trust_state: CapabilityTrustStatus
    verification_status: CapabilityVerificationStatus
    evaluated_at_ms: int
    policy_fingerprint: str

    @property
    def blocking_codes(self) -> tuple[str, ...]:
        findings = (*self.static_findings, *self.dependency_findings, *self.injection_findings)
        return tuple(sorted({finding.code for finding in findings if finding.blocking}))

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "candidate_identity": self.candidate_identity,
            "pin": self.pin,
            "digest": self.digest,
            "provenance": _safe_json(self.provenance),
            "permissions": list(self.permissions),
            "static_findings": [item.to_dict() for item in self.static_findings],
            "dependency_findings": [item.to_dict() for item in self.dependency_findings],
            "injection_findings": [item.to_dict() for item in self.injection_findings],
            "capability_test": self.capability_test.to_dict() if self.capability_test else None,
            "final_trust_state": self.final_trust_state.value,
            "verification_status": self.verification_status.value,
            "evaluated_at_ms": self.evaluated_at_ms,
            "policy_fingerprint": self.policy_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TrustEvaluation":
        capability_test = value.get("capability_test")
        return cls(
            stable_id=str(value["stable_id"]),
            candidate_identity=str(value["candidate_identity"]),
            pin=str(value["pin"]) if value.get("pin") is not None else None,
            digest=str(value["digest"]),
            provenance=_safe_json(value.get("provenance") or {}),
            permissions=tuple(str(item) for item in value.get("permissions") or []),
            static_findings=tuple(
                TrustFinding.from_dict(item) for item in value.get("static_findings") or []
            ),
            dependency_findings=tuple(
                TrustFinding.from_dict(item) for item in value.get("dependency_findings") or []
            ),
            injection_findings=tuple(
                TrustFinding.from_dict(item) for item in value.get("injection_findings") or []
            ),
            capability_test=(
                CapabilityTestResult.from_dict(capability_test)
                if isinstance(capability_test, dict)
                else None
            ),
            final_trust_state=CapabilityTrustStatus(str(value["final_trust_state"])),
            verification_status=CapabilityVerificationStatus(str(value["verification_status"])),
            evaluated_at_ms=int(value["evaluated_at_ms"]),
            policy_fingerprint=str(value["policy_fingerprint"]),
        )


def _finding(category: str, code: str, message: str) -> TrustFinding:
    return TrustFinding(category=category, code=code, message=message, blocking=True)


def _mutable_or_unpinned(pin: str | None) -> bool:
    if not pin:
        return True
    normalized = pin.strip().lower()
    if normalized in _MUTABLE_PINS:
        return True
    if any(marker in normalized for marker in ("^", "~", "*", ">", "<", "||")):
        return True
    if normalized.endswith((".x", ".latest")):
        return True
    return False


def _unsafe_archive_path(name: str) -> bool:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    return path.is_absolute() or ".." in path.parts or normalized.startswith("/")


def _inspect_archive_content(
    content: bytes,
    *,
    max_unpacked_bytes: int,
    max_members: int = 1000,
) -> tuple[tuple[str, ...], tuple[TrustFinding, ...]]:
    """Inspect text inside tar/zip archives without extracting files to disk."""
    findings: list[TrustFinding] = []
    texts: list[str] = []
    consumed = 0

    def add_payload(payload: bytes) -> bool:
        nonlocal consumed
        consumed += len(payload)
        if consumed > max_unpacked_bytes:
            findings.append(
                _finding(
                    "archive",
                    "archive_size_limit",
                    "archive expands beyond the configured inspection byte bound",
                )
            )
            return False
        texts.append(payload.decode("utf-8", errors="replace"))
        return True

    if zipfile.is_zipfile(io.BytesIO(content)):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                members = archive.infolist()
                if len(members) > max_members:
                    findings.append(
                        _finding(
                            "archive",
                            "archive_member_limit",
                            "archive contains too many members to inspect safely",
                        )
                    )
                    return tuple(texts), tuple(findings)
                for member in members:
                    if _unsafe_archive_path(member.filename):
                        findings.append(
                            _finding(
                                "archive",
                                "unsafe_archive_entry",
                                "archive contains an absolute or parent-traversal path",
                            )
                        )
                        continue
                    mode = (member.external_attr >> 16) & 0xFFFF
                    if stat.S_ISLNK(mode) or bool(member.flag_bits & 0x1):
                        findings.append(
                            _finding(
                                "archive",
                                "unsafe_archive_entry",
                                "archive contains a symlink or encrypted member",
                            )
                        )
                        continue
                    if member.is_dir():
                        continue
                    if member.file_size < 0 or consumed + member.file_size > max_unpacked_bytes:
                        findings.append(
                            _finding(
                                "archive",
                                "archive_size_limit",
                                "archive expands beyond the configured inspection byte bound",
                            )
                        )
                        break
                    with archive.open(member, "r") as handle:
                        payload = handle.read(max_unpacked_bytes - consumed + 1)
                    if not add_payload(payload):
                        break
        except (OSError, RuntimeError, zipfile.BadZipFile):
            findings.append(
                _finding("archive", "invalid_archive", "zip archive could not be safely inspected")
            )
        return tuple(texts), tuple(findings)

    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:*")
    except (tarfile.ReadError, OSError):
        if content.startswith(b"\x1f\x8b"):
            findings.append(
                _finding(
                    "archive",
                    "invalid_archive",
                    "compressed tar archive could not be safely inspected",
                )
            )
            return tuple(texts), tuple(findings)
        return (content.decode("utf-8", errors="replace"),), ()

    try:
        with archive:
            members = archive.getmembers()
            if len(members) > max_members:
                findings.append(
                    _finding(
                        "archive",
                        "archive_member_limit",
                        "archive contains too many members to inspect safely",
                    )
                )
                return tuple(texts), tuple(findings)
            for member in members:
                if _unsafe_archive_path(member.name):
                    findings.append(
                        _finding(
                            "archive",
                            "unsafe_archive_entry",
                            "archive contains an absolute or parent-traversal path",
                        )
                    )
                    continue
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    findings.append(
                        _finding(
                            "archive",
                            "unsafe_archive_entry",
                            "archive contains a link, device, or FIFO member",
                        )
                    )
                    continue
                if not member.isfile():
                    continue
                if member.size < 0 or consumed + member.size > max_unpacked_bytes:
                    findings.append(
                        _finding(
                            "archive",
                            "archive_size_limit",
                            "archive expands beyond the configured inspection byte bound",
                        )
                    )
                    break
                handle = archive.extractfile(member)
                if handle is None:
                    findings.append(
                        _finding(
                            "archive",
                            "invalid_archive",
                            "archive member could not be read for inspection",
                        )
                    )
                    continue
                with handle:
                    payload = handle.read(max_unpacked_bytes - consumed + 1)
                if not add_payload(payload):
                    break
    except (OSError, tarfile.TarError):
        findings.append(
            _finding("archive", "invalid_archive", "tar archive could not be safely inspected")
        )
    return tuple(texts), tuple(findings)


def _scan_injection(text: str) -> tuple[TrustFinding, ...]:
    if any(pattern.search(text) for pattern in _INJECTION_PATTERNS):
        return (
            _finding(
                "prompt_injection",
                "prompt_injection",
                "candidate contains instruction text attempting to override trusted policy or reveal secrets",
            ),
        )
    return ()


def _scan_static(text: str) -> tuple[TrustFinding, ...]:
    findings: list[TrustFinding] = []
    if any(pattern.search(text) for pattern in _DANGEROUS_STATIC_PATTERNS):
        findings.append(
            _finding(
                "static_security",
                "dangerous_static_instruction",
                "candidate contains destructive, privilege-escalating, or credential-sensitive instructions",
            )
        )
    if any(pattern.search(text) for pattern in _UNSAFE_INSTALL_PATTERNS):
        findings.append(
            _finding(
                "supply_chain",
                "unsafe_install_instruction",
                "candidate contains an unsafe or mutable remote installation instruction",
            )
        )
    return tuple(findings)


def _dependency_findings_from_json(text: str) -> tuple[TrustFinding, ...]:
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return ()
    if not isinstance(parsed, dict):
        return ()
    findings: list[TrustFinding] = []
    for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        dependencies = parsed.get(section)
        if not isinstance(dependencies, dict):
            continue
        for name, version in list(dependencies.items())[:500]:
            spec = str(version).strip()
            if not _EXACT_SEMVER.fullmatch(spec):
                findings.append(
                    _finding(
                        "dependency",
                        "unpinned_dependency",
                        f"dependency {str(name)[:120]} is not pinned to an exact version",
                    )
                )
                break
    return tuple(findings)


def _dependency_findings_from_install_lines(text: str) -> tuple[TrustFinding, ...]:
    findings: list[TrustFinding] = []
    for line in text.splitlines()[:5000]:
        stripped = line.strip()
        lowered = stripped.lower()
        if re.match(r"^pip(?:3)?\s+install\s+", lowered):
            args = re.sub(r"^pip(?:3)?\s+install\s+", "", stripped, flags=re.IGNORECASE)
            if "git+" in args.lower() or "==" not in args:
                findings.append(
                    _finding(
                        "dependency",
                        "unpinned_dependency",
                        "pip install instruction is not pinned to an exact package version",
                    )
                )
                break
        if re.match(r"^(?:npm\s+(?:install|i)|pnpm\s+add|yarn\s+add)\s+", lowered):
            package_text = re.sub(
                r"^(?:npm\s+(?:install|i)|pnpm\s+add|yarn\s+add)\s+",
                "",
                stripped,
                flags=re.IGNORECASE,
            )
            tokens = [token for token in package_text.split() if not token.startswith("-")]
            if any(not _npm_token_is_pinned(token) for token in tokens):
                findings.append(
                    _finding(
                        "dependency",
                        "unpinned_dependency",
                        "JavaScript package install instruction is not pinned to an exact version",
                    )
                )
                break
    return tuple(findings)


def _npm_token_is_pinned(token: str) -> bool:
    if token.startswith(("http://", "https://", "git+", "github:")):
        return False
    if token.startswith("@"):
        slash = token.find("/")
        if slash < 0:
            return False
        version_at = token.rfind("@")
        if version_at <= slash:
            return False
        version = token[version_at + 1 :]
    else:
        if "@" not in token:
            return False
        version = token.rsplit("@", 1)[1]
    return bool(_EXACT_SEMVER.fullmatch(version))


class FactoryTrustEvaluator:
    def __init__(
        self,
        *,
        capability_tester: CapabilityTestAdapter | None,
        clock_ms: Any = lambda: int(time.time() * 1000),
    ) -> None:
        self._capability_tester = capability_tester
        self._clock_ms = clock_ms

    async def evaluate(self, candidate: TrustCandidate, policy: TrustPolicy) -> TrustEvaluation:
        static_findings: list[TrustFinding] = []
        dependency_findings: list[TrustFinding] = []
        injection_findings: list[TrustFinding] = []
        discovery = candidate.discovery
        artifact = candidate.artifact
        manifest = candidate.manifest

        if (
            artifact.stable_id != discovery.stable_id
            or artifact.candidate_identity != discovery.identity
        ):
            static_findings.append(
                _finding(
                    "integrity",
                    "candidate_identity_mismatch",
                    "quarantine artifact identity does not match discovery record",
                )
            )
        if manifest.stable_id != discovery.stable_id:
            static_findings.append(
                _finding(
                    "integrity",
                    "manifest_identity_mismatch",
                    "capability manifest identity does not match discovery record",
                )
            )

        requires_pin = discovery.candidate_type not in {"documentation", "research_source"}
        if (
            policy.require_pinned_external
            and requires_pin
            and _mutable_or_unpinned(discovery.pinned_version_or_commit)
        ):
            static_findings.append(
                _finding(
                    "provenance",
                    "mutable_or_unpinned_source",
                    "external executable capability is not pinned to an immutable version or commit",
                )
            )

        origin = urlsplit(discovery.origin_uri)
        if origin.scheme != "https":
            static_findings.append(
                _finding(
                    "provenance", "insecure_origin", "external capability origin must use HTTPS"
                )
            )

        content: bytes | None = None
        try:
            content = artifact.read_bytes(max_bytes=policy.max_artifact_bytes)
        except (OSError, ValueError):
            static_findings.append(
                _finding(
                    "integrity",
                    "artifact_integrity_failure",
                    "quarantine artifact failed bounded digest revalidation",
                )
            )

        if discovery.expected_digest and artifact.digest != discovery.expected_digest:
            static_findings.append(
                _finding(
                    "integrity",
                    "digest_mismatch",
                    "quarantine digest does not match provider-published SHA-256",
                )
            )

        discovered_permissions = set(discovery.permissions)
        manifest_permissions = {
            item.strip().lower() for item in manifest.permissions if item.strip()
        }
        if discovered_permissions and not manifest_permissions.issubset(discovered_permissions):
            static_findings.append(
                _finding(
                    "permissions",
                    "permission_escalation",
                    "fetched manifest requests permissions not present in discovery metadata",
                )
            )
        excess = manifest_permissions - set(policy.allowed_permissions)
        if excess:
            static_findings.append(
                _finding(
                    "permissions",
                    "excessive_permission",
                    "candidate requests permissions outside mission policy",
                )
            )
        if not policy.allow_network and (
            "network:http" in manifest_permissions or bool(manifest.network_requirements)
        ):
            static_findings.append(
                _finding(
                    "permissions",
                    "network_permission_denied",
                    "candidate requires network access but mission policy disallows it",
                )
            )

        if content is not None:
            inspection_texts, archive_findings = _inspect_archive_content(
                content,
                max_unpacked_bytes=policy.max_artifact_bytes,
            )
            static_findings.extend(archive_findings)
            for text in inspection_texts:
                static_findings.extend(_scan_static(text))
                dependency_findings.extend(_dependency_findings_from_json(text))
                dependency_findings.extend(_dependency_findings_from_install_lines(text))
                injection_findings.extend(_scan_injection(text))

        capability_test: CapabilityTestResult | None = None
        blocking = any(
            finding.blocking
            for finding in (*static_findings, *dependency_findings, *injection_findings)
        )
        if blocking:
            final_state = CapabilityTrustStatus.REJECTED
            verification_status = CapabilityVerificationStatus.UNVERIFIED
        elif policy.require_capability_test:
            if self._capability_tester is None:
                final_state = CapabilityTrustStatus.QUARANTINED
                verification_status = CapabilityVerificationStatus.STATIC_VERIFIED
            else:
                try:
                    capability_test = await asyncio.wait_for(
                        self._capability_tester.test(candidate, policy),
                        timeout=policy.capability_test_timeout_ms / 1000,
                    )
                except TimeoutError:
                    static_findings.append(
                        _finding(
                            "capability_test",
                            "capability_test_timeout",
                            "constrained capability test timed out",
                        )
                    )
                    final_state = CapabilityTrustStatus.REJECTED
                    verification_status = CapabilityVerificationStatus.STATIC_VERIFIED
                except Exception as exc:
                    static_findings.append(
                        _finding(
                            "capability_test",
                            "capability_test_error",
                            f"constrained capability test failed with {exc.__class__.__name__}",
                        )
                    )
                    final_state = CapabilityTrustStatus.REJECTED
                    verification_status = CapabilityVerificationStatus.STATIC_VERIFIED
                else:
                    if not isinstance(capability_test, CapabilityTestResult):
                        static_findings.append(
                            _finding(
                                "capability_test",
                                "capability_test_invalid",
                                "capability tester returned an invalid result",
                            )
                        )
                        capability_test = None
                        final_state = CapabilityTrustStatus.REJECTED
                        verification_status = CapabilityVerificationStatus.STATIC_VERIFIED
                    elif capability_test.passed:
                        final_state = CapabilityTrustStatus.APPROVED
                        verification_status = CapabilityVerificationStatus.CAPABILITY_TESTED
                    else:
                        static_findings.append(
                            _finding(
                                "capability_test",
                                "capability_test_failed",
                                "constrained capability test did not verify the capability",
                            )
                        )
                        final_state = CapabilityTrustStatus.REJECTED
                        verification_status = CapabilityVerificationStatus.STATIC_VERIFIED
        else:
            final_state = CapabilityTrustStatus.APPROVED
            verification_status = CapabilityVerificationStatus.STATIC_VERIFIED

        return TrustEvaluation(
            stable_id=discovery.stable_id,
            candidate_identity=discovery.identity,
            pin=discovery.pinned_version_or_commit,
            digest=artifact.digest,
            provenance={
                "provider": discovery.provider,
                "candidate_type": discovery.candidate_type,
                "origin_uri": discovery.origin_uri,
                "pin": discovery.pinned_version_or_commit,
            },
            permissions=tuple(sorted(manifest_permissions)),
            static_findings=tuple(static_findings),
            dependency_findings=tuple(dependency_findings),
            injection_findings=tuple(injection_findings),
            capability_test=capability_test,
            final_trust_state=final_state,
            verification_status=verification_status,
            evaluated_at_ms=int(self._clock_ms()),
            policy_fingerprint=policy.fingerprint,
        )


class ApprovedTrustCache:
    """Durable metadata cache for still-valid APPROVED trust decisions."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else DATA_DIR / "factory-approved-capabilities"

    @staticmethod
    def _cache_key(
        *,
        stable_id: str,
        pin: str | None,
        digest: str,
        policy_fingerprint: str,
    ) -> str:
        raw = f"{stable_id}\0{pin or ''}\0{digest}\0{policy_fingerprint}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def put(self, evaluation: TrustEvaluation) -> Path:
        if evaluation.final_trust_state != CapabilityTrustStatus.APPROVED:
            raise ValueError("only APPROVED trust evaluations may enter the approved cache")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        key = self._cache_key(
            stable_id=evaluation.stable_id,
            pin=evaluation.pin,
            digest=evaluation.digest,
            policy_fingerprint=evaluation.policy_fingerprint,
        )
        path = self.root / f"{key}.json"
        raw = json.dumps(evaluation.to_dict(), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        temp = self.root / f".{key}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)
        os.chmod(path, 0o600)
        return path

    def get(
        self,
        candidate: TrustCandidate,
        policy: TrustPolicy,
        *,
        now_ms: int | None = None,
    ) -> TrustEvaluation | None:
        try:
            candidate.artifact.read_bytes(max_bytes=policy.max_artifact_bytes)
        except (OSError, ValueError):
            return None
        key = self._cache_key(
            stable_id=candidate.discovery.stable_id,
            pin=candidate.discovery.pinned_version_or_commit,
            digest=candidate.artifact.digest,
            policy_fingerprint=policy.fingerprint,
        )
        path = self.root / f"{key}.json"
        try:
            raw = path.read_text(encoding="utf-8")
            evaluation = TrustEvaluation.from_dict(json.loads(raw))
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        if evaluation.final_trust_state != CapabilityTrustStatus.APPROVED:
            return None
        if evaluation.stable_id != candidate.discovery.stable_id:
            return None
        if evaluation.candidate_identity != candidate.discovery.identity:
            return None
        if evaluation.pin != candidate.discovery.pinned_version_or_commit:
            return None
        if evaluation.digest != candidate.artifact.digest:
            return None
        if evaluation.policy_fingerprint != policy.fingerprint:
            return None
        if current_ms - evaluation.evaluated_at_ms > policy.cache_ttl_ms:
            return None
        return evaluation
