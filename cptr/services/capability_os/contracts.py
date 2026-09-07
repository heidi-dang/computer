"""Typed, content-addressed Capability OS contracts.

These objects describe computational artefacts and requested authority. They
never grant authority by themselves; that remains the Authority Broker's job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar


class ArtifactKind(str, Enum):
    TOOL = "Tool"
    SKILL = "Skill"
    CAPABILITY = "Capability"
    PLUGIN = "Plugin"
    MCP_ADAPTER = "McpAdapter"


class ArtifactOwner(str, Enum):
    CPTR = "cptr"
    USER = "user"
    GENERATED = "generated"
    EXTERNAL = "external"


class ArtifactOrigin(str, Enum):
    CORE = "core"
    FORGE = "forge"
    LEARNED = "learned"
    PLUGIN = "plugin"
    MCP = "mcp"
    IMPORTED = "imported"


class ArtifactState(str, Enum):
    EPHEMERAL = "ephemeral"
    QUALIFIED = "qualified"
    LEARNED = "learned"
    CERTIFIED = "certified"
    CORE = "core"
    RETIRED = "retired"


@dataclass(frozen=True)
class CapabilityRequest:
    action: str
    resource: str
    constraints: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action = str(self.action).strip().lower()
        resource = str(self.resource).strip()
        if not action:
            raise ValueError("capability action must not be blank")
        if not resource:
            raise ValueError("capability resource must not be blank")
        if not isinstance(self.constraints, dict):
            raise TypeError("capability constraints must be an object")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "constraints", _json_safe(self.constraints))

    @property
    def key(self) -> tuple[str, str]:
        return self.action, self.resource

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"action": self.action, "resource": self.resource}
        if self.constraints:
            payload["constraints"] = _json_safe(self.constraints)
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CapabilityRequest":
        return cls(
            action=str(value.get("action") or ""),
            resource=str(value.get("resource") or ""),
            constraints=dict(value.get("constraints") or {}),
        )


@dataclass(frozen=True)
class ArtifactMetadata:
    id: str
    version: str
    owner: ArtifactOwner
    origin: ArtifactOrigin
    created_at: str
    content_digest: str
    user_id: str | None = None
    parent: str | None = None
    task_origin: str | None = None
    source_digest: str | None = None


TSpec = TypeVar("TSpec")


@dataclass(frozen=True)
class CptrArtifact(Generic[TSpec]):
    api_version: str
    kind: ArtifactKind
    metadata: ArtifactMetadata
    compatibility: dict[str, Any]
    spec: TSpec
    state: ArtifactState = ArtifactState.EPHEMERAL

    @property
    def identity(self) -> str:
        return f"{self.metadata.id}@{self.metadata.version}#{self.metadata.content_digest}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "apiVersion": self.api_version,
            "kind": self.kind.value,
            "metadata": {
                "id": self.metadata.id,
                "version": self.metadata.version,
                "owner": self.metadata.owner.value,
                "origin": self.metadata.origin.value,
                "createdAt": self.metadata.created_at,
                "userId": self.metadata.user_id,
                "parent": self.metadata.parent,
                "taskOrigin": self.metadata.task_origin,
                "sourceDigest": self.metadata.source_digest,
                "contentDigest": self.metadata.content_digest,
            },
            "compatibility": _json_safe(self.compatibility),
            "spec": _json_safe(self.spec),
            "state": self.state.value,
        }


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        raise ValueError("Capability OS payload nesting exceeds limit")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key)
            if not text:
                raise ValueError("Capability OS object keys must not be blank")
            result[text] = _json_safe(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth=depth + 1) for item in value]
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_safe(value.to_dict(), depth=depth + 1)
    raise TypeError(f"unsupported Capability OS value: {value.__class__.__name__}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest_payload(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def create_artifact(
    *,
    artifact_id: str,
    version: str,
    kind: ArtifactKind,
    owner: ArtifactOwner,
    origin: ArtifactOrigin,
    spec: Any,
    created_at: str,
    compatibility: dict[str, Any] | None = None,
    user_id: str | None = None,
    parent: str | None = None,
    task_origin: str | None = None,
    source_digest: str | None = None,
    state: ArtifactState = ArtifactState.EPHEMERAL,
) -> CptrArtifact[Any]:
    normalized_id = str(artifact_id).strip()
    normalized_version = str(version).strip()
    normalized_created_at = str(created_at).strip()
    if not normalized_id or not normalized_version or not normalized_created_at:
        raise ValueError("artifact id, version, and created_at must not be blank")
    compatibility = dict(compatibility or {"cptr": ">=0.9"})
    normalized_user_id = str(user_id).strip() if user_id is not None else None
    if user_id is not None and not normalized_user_id:
        raise ValueError("artifact user_id must not be blank")
    safe_spec = _json_safe(spec)
    content = {
        "apiVersion": "cptr.io/v1alpha1",
        "kind": kind.value,
        "metadata": {
            "id": normalized_id,
            "version": normalized_version,
            "owner": owner.value,
            "origin": origin.value,
            "scopeOwnerId": normalized_user_id,
            "parent": parent,
            "sourceDigest": source_digest,
        },
        "compatibility": compatibility,
        "spec": safe_spec,
    }
    digest = digest_payload(content)
    return CptrArtifact(
        api_version="cptr.io/v1alpha1",
        kind=kind,
        metadata=ArtifactMetadata(
            id=normalized_id,
            version=normalized_version,
            owner=owner,
            origin=origin,
            created_at=normalized_created_at,
            user_id=normalized_user_id,
            parent=parent,
            task_origin=task_origin,
            source_digest=source_digest,
            content_digest=digest,
        ),
        compatibility=_json_safe(compatibility),
        spec=safe_spec,
        state=state,
    )
