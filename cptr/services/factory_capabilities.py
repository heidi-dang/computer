"""Normalized local capability inventory for the Dark Factory.

Discovery in this module is metadata-only. It never connects to MCP servers,
executes skills, starts processes, or invokes tools. The resulting manifests are
safe inputs to later trust/ranking stages, not permission to execute anything.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit, urlunsplit

from cptr.utils.skills import SkillMeta, discover_skills


class CapabilityTrustStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    FETCHED = "FETCHED"
    PINNED = "PINNED"
    QUARANTINED = "QUARANTINED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    REVOKED = "REVOKED"
    STALE_REVIEW_REQUIRED = "STALE_REVIEW_REQUIRED"


class CapabilityVerificationStatus(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    LOCAL = "LOCAL"
    STATIC_VERIFIED = "STATIC_VERIFIED"
    CAPABILITY_TESTED = "CAPABILITY_TESTED"


@dataclass(frozen=True)
class CapabilityManifest:
    stable_id: str
    version: str
    origin_type: str
    origin_uri: str
    pinned_version_or_commit: str | None
    digest: str
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...]
    network_requirements: tuple[str, ...]
    execution_requirements: tuple[str, ...]
    risk_classification: str
    trust_status: CapabilityTrustStatus
    verification_status: CapabilityVerificationStatus
    maintenance_metadata: dict[str, Any] = field(default_factory=dict)
    historical_factory_score: float | None = None
    created_at: int | None = None
    evaluated_at: int | None = None

    @property
    def identity(self) -> str:
        return f"{self.stable_id}:{self.version}:{self.digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "version": self.version,
            "origin_type": self.origin_type,
            "origin_uri": self.origin_uri,
            "pinned_version_or_commit": self.pinned_version_or_commit,
            "digest": self.digest,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "network_requirements": list(self.network_requirements),
            "execution_requirements": list(self.execution_requirements),
            "risk_classification": self.risk_classification,
            "trust_status": self.trust_status.value,
            "verification_status": self.verification_status.value,
            "maintenance_metadata": _safe_metadata(self.maintenance_metadata),
            "historical_factory_score": self.historical_factory_score,
            "created_at": self.created_at,
            "evaluated_at": self.evaluated_at,
        }


@dataclass(frozen=True)
class CapabilityRequirement:
    requirement_id: str
    capabilities: tuple[str, ...]
    required_permissions: tuple[str, ...]
    network_allowed: bool

    @classmethod
    def create(
        cls,
        *,
        requirement_id: str,
        capabilities: Iterable[str],
        required_permissions: Iterable[str],
        network_allowed: bool,
    ) -> "CapabilityRequirement":
        normalized_id = requirement_id.strip()
        if not normalized_id:
            raise ValueError("capability requirement ID must not be blank")
        normalized_capabilities = _normalized_tokens(capabilities, "capability")
        if not normalized_capabilities:
            raise ValueError("capability requirement must contain at least one capability")
        normalized_permissions = _normalized_tokens(required_permissions, "permission")
        return cls(
            requirement_id=normalized_id,
            capabilities=normalized_capabilities,
            required_permissions=normalized_permissions,
            network_allowed=bool(network_allowed),
        )


def _normalized_tokens(values: Iterable[str], label: str) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in values:
        value = str(raw).strip().lower()
        if not value:
            raise ValueError(f"{label} must not be blank")
        normalized.add(value)
    return tuple(sorted(normalized))


def _digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_id(kind: str, logical_name: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{logical_name}".encode("utf-8")).hexdigest()[:24]
    return f"cap_{kind}_{digest}"


def _safe_metadata(value: dict[str, Any]) -> dict[str, Any]:
    blocked_fragments = {
        "authorization",
        "cookie",
        "credential",
        "env",
        "header",
        "password",
        "secret",
        "token",
    }
    safe: dict[str, Any] = {}
    for key, item in value.items():
        key_text = str(key)
        if any(fragment in key_text.lower() for fragment in blocked_fragments):
            continue
        if isinstance(item, dict):
            safe[key_text] = _safe_metadata(item)
        elif isinstance(item, (str, int, float, bool)) or item is None:
            safe[key_text] = item
        elif isinstance(item, (list, tuple)):
            safe[key_text] = [
                _safe_metadata(entry) if isinstance(entry, dict) else entry
                for entry in item[:100]
                if isinstance(entry, (dict, str, int, float, bool)) or entry is None
            ]
    return safe


def _sanitize_http_origin(raw_url: str) -> str:
    parsed = urlsplit(raw_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("configured MCP URL must be http(s)")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (parsed.scheme == "https" and parsed.port == 443) or (
        parsed.scheme == "http" and parsed.port == 80
    )
    netloc = host if parsed.port is None or default_port else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path or "", "", ""))


def _tool_permissions(allowed_tools: str | None) -> tuple[str, ...]:
    if not allowed_tools:
        return ("workspace:read",)
    raw_tools = [token for token in re.split(r"[\s,]+", allowed_tools.strip()) if token]
    permissions: set[str] = set()
    for raw in raw_tools:
        tool = raw.lower()
        if any(marker in tool for marker in ("read", "search", "grep", "fdx", "lsp")):
            permissions.add("workspace:read")
        if any(marker in tool for marker in ("write", "edit", "patch", "move", "delete")):
            permissions.add("workspace:write")
        if any(marker in tool for marker in ("web", "browser", "http", "fetch")):
            permissions.add("network:http")
        if any(marker in tool for marker in ("command", "terminal", "shell", "pty", "exec")):
            permissions.add("process:execute")
    if not permissions:
        permissions.add("workspace:read")
    return tuple(sorted(permissions))


_BUILTIN_MANIFESTS: tuple[dict[str, Any], ...] = (
    {
        "name": "cptr-direct-coding",
        "capabilities": ("code-read", "code-search", "code-edit", "workspace-inspection"),
        "permissions": ("workspace:read", "workspace:write"),
        "execution_requirements": ("cptr-direct-coding",),
    },
    {
        "name": "fdx-repository-intelligence",
        "capabilities": ("code-search", "dependency-impact", "impact-analysis", "repo-analysis"),
        "permissions": ("workspace:read",),
        "execution_requirements": ("fdx",),
    },
    {
        "name": "lsp-language-intelligence",
        "capabilities": ("definition", "references", "symbols", "language-analysis"),
        "permissions": ("workspace:read", "process:execute"),
        "execution_requirements": ("lsp",),
    },
    {
        "name": "command-execution",
        "capabilities": ("command", "test-execution", "build-execution"),
        "permissions": ("process:execute", "workspace:read"),
        "execution_requirements": ("command-service",),
    },
    {
        "name": "managed-browser",
        "capabilities": ("browser", "live-runtime-verification", "web-research"),
        "permissions": ("network:http",),
        "network_requirements": ("external-http",),
        "execution_requirements": ("managed-browser",),
    },
    {
        "name": "git-operations",
        "capabilities": ("diff", "git", "revision-inspection"),
        "permissions": ("workspace:read", "workspace:write"),
        "execution_requirements": ("git-service",),
    },
)


class CapabilityInventory:
    def __init__(
        self,
        *,
        skill_discoverer: Callable[[str], Any] = discover_skills,
        mcp_server_loader: Callable[[], Any] | None = None,
        include_builtins: bool = True,
        skill_digest_loader: Callable[[str], str] | None = None,
    ) -> None:
        self._skill_discoverer = skill_discoverer
        self._mcp_server_loader = mcp_server_loader or self._load_configured_mcp_servers
        self._include_builtins = include_builtins
        self._skill_digest_loader = skill_digest_loader or self._read_skill_digest

    async def discover_local(self, workspace: str) -> list[CapabilityManifest]:
        manifests: list[CapabilityManifest] = []
        skills = await _resolve(self._skill_discoverer(workspace))
        for skill in skills or []:
            manifests.append(await self._skill_manifest(skill))
        servers = await _resolve(self._mcp_server_loader())
        for server in servers or []:
            if isinstance(server, dict) and server.get("type") in {"mcp", "mcp_stdio"}:
                manifests.append(self._mcp_manifest(server))
        if self._include_builtins:
            manifests.extend(self._builtin_manifests())
        return normalize_manifests(manifests)

    async def _skill_manifest(self, skill: SkillMeta) -> CapabilityManifest:
        digest = await _resolve(self._skill_digest_loader(skill.location))
        if not isinstance(digest, str) or not digest.strip():
            raise ValueError("skill digest loader returned an invalid digest")
        logical_name = f"{skill.source}:{skill.name.strip().lower()}"
        permissions = _tool_permissions(skill.allowed_tools)
        metadata = _safe_metadata(skill.metadata or {})
        version = str(metadata.get("version") or "local")
        return CapabilityManifest(
            stable_id=_stable_id("skill", logical_name),
            version=version,
            origin_type="skill",
            origin_uri=f"skill:{skill.source}:{skill.name}",
            pinned_version_or_commit=None,
            digest=digest.strip(),
            capabilities=tuple(sorted({skill.name.strip().lower(), "skill-instructions"})),
            permissions=permissions,
            network_requirements=("external-http",) if "network:http" in permissions else (),
            execution_requirements=("skill-instructions",),
            risk_classification="LOCAL_MANAGED" if skill.managed else "LOCAL_UNMANAGED",
            trust_status=(
                CapabilityTrustStatus.APPROVED
                if skill.managed
                else CapabilityTrustStatus.DISCOVERED
            ),
            verification_status=CapabilityVerificationStatus.LOCAL,
            maintenance_metadata={
                "source": skill.source,
                "managed": bool(skill.managed),
                "license": skill.license,
                "compatibility": skill.compatibility,
                "view_count": int(skill.view_count),
                "use_count": int(skill.use_count),
                "update_count": int(skill.update_count),
                "last_updated_at": skill.last_updated_at,
                **metadata,
            },
        )

    def _mcp_manifest(self, server: dict[str, Any]) -> CapabilityManifest:
        server_type = str(server.get("type") or "")
        server_id = str(server.get("id") or server.get("name") or "unnamed").strip()
        if not server_id:
            raise ValueError("configured MCP server must have an ID or name")
        version = str(server.get("version") or "configured")
        if server_type == "mcp":
            origin_uri = _sanitize_http_origin(str(server.get("url") or ""))
            permissions = ("network:http",)
            network_requirements = ("external-http",)
            execution_requirements = ("mcp-client",)
        else:
            origin_uri = f"stdio:{server_id}"
            permissions = ("process:execute",)
            network_requirements = ()
            execution_requirements = ("mcp-stdio-client",)
        safe_identity = {
            "id": server_id,
            "name": str(server.get("name") or server_id),
            "type": server_type,
            "origin_uri": origin_uri,
            "version": version,
        }
        digest = _digest_bytes(
            json.dumps(safe_identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        return CapabilityManifest(
            stable_id=_stable_id("mcp", f"{server_type}:{server_id}"),
            version=version,
            origin_type="mcp",
            origin_uri=origin_uri,
            pinned_version_or_commit=version if version != "configured" else None,
            digest=digest,
            capabilities=("mcp-tools",),
            permissions=permissions,
            network_requirements=network_requirements,
            execution_requirements=execution_requirements,
            risk_classification="CONFIGURED_LOCAL_PROVIDER",
            trust_status=CapabilityTrustStatus.APPROVED,
            verification_status=CapabilityVerificationStatus.LOCAL,
            maintenance_metadata={"server_id": server_id, "server_type": server_type},
        )

    @staticmethod
    def _builtin_manifests() -> list[CapabilityManifest]:
        manifests: list[CapabilityManifest] = []
        for item in _BUILTIN_MANIFESTS:
            name = str(item["name"])
            safe = {
                "name": name,
                "capabilities": list(item["capabilities"]),
                "permissions": list(item["permissions"]),
                "network_requirements": list(item.get("network_requirements", ())),
                "execution_requirements": list(item["execution_requirements"]),
            }
            manifests.append(
                CapabilityManifest(
                    stable_id=_stable_id("builtin", name),
                    version="builtin-v1",
                    origin_type="builtin",
                    origin_uri=f"cptr:{name}",
                    pinned_version_or_commit="builtin-v1",
                    digest=_digest_bytes(
                        json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    ),
                    capabilities=tuple(sorted(item["capabilities"])),
                    permissions=tuple(sorted(item["permissions"])),
                    network_requirements=tuple(sorted(item.get("network_requirements", ()))),
                    execution_requirements=tuple(sorted(item["execution_requirements"])),
                    risk_classification="CPTR_BUILTIN",
                    trust_status=CapabilityTrustStatus.APPROVED,
                    verification_status=CapabilityVerificationStatus.LOCAL,
                    maintenance_metadata={"provider": "cptr"},
                )
            )
        return manifests

    @staticmethod
    async def _load_configured_mcp_servers() -> list[dict[str, Any]]:
        from cptr.models import Config

        value = await Config.get("tool_servers")
        return list(value) if isinstance(value, list) else []

    @staticmethod
    def _read_skill_digest(path: str) -> str:
        return _digest_bytes(Path(path).read_bytes())


def normalize_manifests(manifests: Iterable[CapabilityManifest]) -> list[CapabilityManifest]:
    by_identity: dict[str, CapabilityManifest] = {}
    for manifest in manifests:
        existing = by_identity.get(manifest.identity)
        if existing is None:
            by_identity[manifest.identity] = manifest
            continue
        # Same immutable identity must normalize to one deterministic representation.
        if json.dumps(existing.to_dict(), sort_keys=True, default=str) != json.dumps(
            manifest.to_dict(), sort_keys=True, default=str
        ):
            winner = min(
                (existing, manifest),
                key=lambda item: json.dumps(item.to_dict(), sort_keys=True, default=str),
            )
            by_identity[manifest.identity] = winner
    return [by_identity[key] for key in sorted(by_identity)]


async def _resolve(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value
