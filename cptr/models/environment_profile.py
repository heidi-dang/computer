"""Durable Environment Profiles and immutable, versioned Environment Profile Versions."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.sqlite import JSON

from cptr.models.base import Base


class EnvironmentProfileVersionImmutableError(RuntimeError):
    """Raised when attempting to modify an immutable EnvironmentProfileVersion."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def compute_version_digest(
    *,
    runtime_profile: str,
    environment_variables: dict[str, str],
    packages: dict[str, Any] | list[Any],
    settings: dict[str, Any],
    credential_refs: list[dict[str, Any]],
) -> str:
    """Compute a deterministic SHA-256 digest for an immutable profile version payload."""
    payload = {
        "runtime_profile": str(runtime_profile or "default").strip(),
        "environment_variables": {
            str(k).strip(): str(v) for k, v in sorted((environment_variables or {}).items())
        },
        "packages": packages if packages is not None else {},
        "settings": settings if settings is not None else {},
        "credential_refs": sorted(
            credential_refs or [],
            key=lambda r: str(r.get("logical_name") or r.get("logicalName") or ""),
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EnvironmentProfile(Base):
    """Logical environment profile identifying runtime environment configuration."""

    __tablename__ = "environment_profiles"

    id = Column(Text, primary_key=True, default=lambda: _id("envprof"))
    user_id = Column(Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id = Column(Text, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True)
    name = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    active_version_id = Column(Text, nullable=True)
    is_archived = Column(Boolean, nullable=False, default=False)
    target_name = Column(
        Text, nullable=True
    )  # stable environment target identifier (e.g. 'local', 'aws', 'production')
    created_at_ms = Column(BigInteger, nullable=False, default=lambda: int(time.time() * 1000))
    updated_at_ms = Column(BigInteger, nullable=False, default=lambda: int(time.time() * 1000))

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_environment_profiles_user_name"),
        Index("ix_environment_profiles_user", "user_id"),
        Index("ix_environment_profiles_workspace", "workspace_id"),
        Index("ix_environment_profiles_user_archived", "user_id", "is_archived"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "active_version_id": self.active_version_id,
            "is_archived": self.is_archived,
            "target_name": self.target_name,
            "created_at_ms": self.created_at_ms,
            "updated_at_ms": self.updated_at_ms,
        }


class EnvironmentProfileVersion(Base):
    """Immutable, versioned snapshot of an environment profile specification.

    Once created, rows in this table must never be mutated. New configurations
    must be published as new version rows with incremented version numbers.
    """

    __tablename__ = "environment_profile_versions"

    id = Column(Text, primary_key=True, default=lambda: _id("envver"))
    profile_id = Column(
        Text, ForeignKey("environment_profiles.id", ondelete="CASCADE"), nullable=False
    )
    version_number = Column(BigInteger, nullable=False)
    digest = Column(Text, nullable=False)
    runtime_profile = Column(Text, nullable=False, default="default")
    environment_variables = Column(JSON, nullable=False, default=dict)
    packages = Column(JSON, nullable=False, default=dict)
    settings = Column(JSON, nullable=False, default=dict)
    credential_refs = Column(JSON, nullable=False, default=list)
    parent_version_id = Column(
        Text, ForeignKey("environment_profile_versions.id", ondelete="SET NULL"), nullable=True
    )
    created_by = Column(Text, nullable=True)
    created_at_ms = Column(BigInteger, nullable=False, default=lambda: int(time.time() * 1000))

    __table_args__ = (
        UniqueConstraint("profile_id", "version_number", name="uq_env_profile_version_number"),
        UniqueConstraint("profile_id", "digest", name="uq_env_profile_version_digest"),
        Index("ix_env_profile_versions_profile", "profile_id"),
        Index("ix_env_profile_versions_digest", "digest"),
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "profile_id": self.profile_id,
            "version_number": self.version_number,
            "digest": self.digest,
            "runtime_profile": self.runtime_profile,
            "environment_variables": self.environment_variables,
            "packages": self.packages,
            "settings": self.settings,
            "credential_refs": self.credential_refs,
            "parent_version_id": self.parent_version_id,
            "created_by": self.created_by,
            "created_at_ms": self.created_at_ms,
        }


@event.listens_for(EnvironmentProfileVersion, "before_update")
def _prevent_profile_version_update(mapper, connection, target):
    raise EnvironmentProfileVersionImmutableError(
        f"EnvironmentProfileVersion '{target.id}' (v{target.version_number}) is immutable and cannot be modified"
    )
