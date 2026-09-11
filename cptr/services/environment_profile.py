"""Service and validation logic for Environment Profiles and immutable Versions."""

from __future__ import annotations

import re
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cptr.models.environment_profile import (
    EnvironmentProfile,
    EnvironmentProfileVersion,
    compute_version_digest,
)
from cptr.services.capability_os.credential_broker import _ALLOWED_ENCRYPTED_CONFIG_KEYS
from cptr.utils.redaction import (
    _BEARER_RE,
    _KNOWN_TOKEN_RE,
    _key_is_sensitive,
)

from cptr.services.environment_target import (
    EnvironmentTarget,
    TargetOwnershipError,
    resolve_target,
    target_registry,
)


class EnvironmentProfileError(Exception):
    """Base error for environment profile operations."""


class EnvironmentProfileNotFoundError(EnvironmentProfileError):
    """Raised when an environment profile is not found."""


class EnvironmentProfileVersionNotFoundError(EnvironmentProfileError):
    """Raised when an environment profile version is not found."""


class RawSecretStorageError(ValueError, EnvironmentProfileError):
    """Raised when raw credentials or sensitive variables are detected in non-broker storage."""


class InvalidCredentialRefError(ValueError, EnvironmentProfileError):
    """Raised when a credential reference fails schema or broker source validation."""


# Supported broker source types
SUPPORTED_CREDENTIAL_SOURCE_TYPES = frozenset(
    {
        "ai_connection",
        "encrypted_config",
        "credential_broker",
        "mcp_oauth",
        "vault",
    }
)

# Forbidden keys in credential reference dictionary that indicate attempts to store raw secrets
FORBIDDEN_RAW_SECRET_KEYS = frozenset(
    {
        "secret",
        "secret_value",
        "raw_secret",
        "raw_key",
        "key",
        "password",
        "token",
        "api_key",
        "apikey",
        "value",
        "val",
        "credential",
        "secret_material",
    }
)

ALLOWED_CREDENTIAL_REF_KEYS = frozenset(
    {
        "logical_name",
        "logicalName",
        "source_type",
        "sourceType",
        "source_ref",
        "sourceRef",
        "consumers",
        "target_env_var",
        "targetEnvVar",
        "description",
    }
)

_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PRIVATE_KEY_HEADER_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def contains_raw_secret_value(value: Any) -> bool:
    """Check whether a value matches known secret formats or high-entropy tokens."""
    if not isinstance(value, str):
        return False
    val = value.strip()
    if not val:
        return False
    if _KNOWN_TOKEN_RE.search(val):
        return True
    if _BEARER_RE.search(val):
        return True
    if _PRIVATE_KEY_HEADER_RE.search(val):
        return True
    return False


def validate_credential_ref(ref: Any) -> dict[str, Any]:
    """Validate a single credential reference dictionary according to broker patterns."""
    if not isinstance(ref, dict):
        raise InvalidCredentialRefError(
            f"Credential reference must be a mapping, got {type(ref).__name__}"
        )

    # Check for forbidden raw secret keys
    found_forbidden = [k for k in ref if k.lower() in FORBIDDEN_RAW_SECRET_KEYS]
    if found_forbidden:
        raise RawSecretStorageError(
            f"Raw secret storage forbidden in credential_refs. Found prohibited key(s): {found_forbidden}. "
            "Credential references must specify source_type and source_ref pointing to an authorized broker source."
        )

    # Check for unexpected keys
    extra_keys = [k for k in ref if k not in ALLOWED_CREDENTIAL_REF_KEYS]
    if extra_keys:
        raise InvalidCredentialRefError(
            f"Unsupported keys in credential reference: {extra_keys}. Allowed: {sorted(ALLOWED_CREDENTIAL_REF_KEYS)}"
        )

    # Extract normalized fields
    logical_name = str(ref.get("logical_name") or ref.get("logicalName") or "").strip()
    source_type = str(ref.get("source_type") or ref.get("sourceType") or "").strip()
    source_ref = str(ref.get("source_ref") or ref.get("sourceRef") or "").strip()
    raw_consumers = ref.get("consumers")
    target_env_var = str(ref.get("target_env_var") or ref.get("targetEnvVar") or "").strip()
    description = str(ref.get("description") or "").strip()

    if not logical_name:
        raise InvalidCredentialRefError("Credential reference logical_name must not be blank")

    if not source_type:
        raise InvalidCredentialRefError("Credential reference source_type must not be blank")

    if source_type not in SUPPORTED_CREDENTIAL_SOURCE_TYPES:
        raise InvalidCredentialRefError(
            f"Unsupported credential source_type '{source_type}'. "
            f"Must be one of: {sorted(SUPPORTED_CREDENTIAL_SOURCE_TYPES)}"
        )

    if not source_ref:
        raise InvalidCredentialRefError("Credential reference source_ref must not be blank")

    # Check that source_ref itself is not a raw secret
    if contains_raw_secret_value(source_ref):
        raise RawSecretStorageError(
            "source_ref contains a raw secret token format; it must be a reference/key, not the secret itself"
        )

    # Reusing broker pattern: encrypted_config source_ref must belong to allowed encrypted keys
    if source_type == "encrypted_config" and source_ref not in _ALLOWED_ENCRYPTED_CONFIG_KEYS:
        raise InvalidCredentialRefError(
            f"source_ref '{source_ref}' is not in allowed encrypted config keys: "
            f"{sorted(_ALLOWED_ENCRYPTED_CONFIG_KEYS)}"
        )

    # Reusing broker pattern: consumers must be a non-empty sequence
    if not isinstance(raw_consumers, (list, tuple, set)):
        raise InvalidCredentialRefError(
            "Credential reference consumers must be a non-empty list of consumer scopes"
        )

    normalized_consumers = tuple(
        dict.fromkeys(str(c).strip() for c in raw_consumers if str(c).strip())
    )
    if not normalized_consumers:
        raise InvalidCredentialRefError("Credential reference requires at least one exact consumer")

    # consumers entries must not contain raw secret material
    for consumer in normalized_consumers:
        if contains_raw_secret_value(consumer):
            raise RawSecretStorageError(
                "A consumer scope entry contains a value matching a raw secret format. "
                "Consumer scopes must be logical identifiers, not credential values."
            )

    # If target_env_var is specified, it must be a valid env var name
    if target_env_var and not _ENV_VAR_NAME_RE.match(target_env_var):
        raise InvalidCredentialRefError(
            f"target_env_var '{target_env_var}' is not a valid environment variable identifier"
        )

    # description must not contain raw secret material
    if description and contains_raw_secret_value(description):
        raise RawSecretStorageError(
            "description contains a value matching a raw secret format. "
            "Use a human-readable label; credential values belong in the broker source."
        )

    return {
        "logical_name": logical_name,
        "source_type": source_type,
        "source_ref": source_ref,
        "consumers": list(normalized_consumers),
        "target_env_var": target_env_var if target_env_var else None,
        "description": description if description else None,
    }


def validate_credential_refs(refs: Any) -> list[dict[str, Any]]:
    """Validate a list of credential references and enforce unique logical names."""
    if refs is None:
        return []
    if not isinstance(refs, list):
        raise InvalidCredentialRefError("credential_refs must be a list")

    validated: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for ref in refs:
        norm = validate_credential_ref(ref)
        name = norm["logical_name"]
        if name in seen_names:
            raise InvalidCredentialRefError(f"Duplicate credential reference logical_name '{name}'")
        seen_names.add(name)
        validated.append(norm)

    return validated


def validate_environment_variables(env_vars: Any) -> dict[str, str]:
    """Validate environment variables, strictly prohibiting sensitive keys and raw secret tokens."""
    if env_vars is None:
        return {}
    if not isinstance(env_vars, dict):
        raise ValueError(
            "environment_variables must be a mapping of variable names to string values"
        )

    validated: dict[str, str] = {}
    for raw_k, raw_v in env_vars.items():
        k = str(raw_k).strip()
        if not k:
            raise ValueError("Environment variable name must not be blank")
        if not _ENV_VAR_NAME_RE.match(k):
            raise ValueError(
                f"Invalid environment variable name '{k}': must match [A-Za-z_][A-Za-z0-9_]*"
            )

        # Prohibit sensitive variable names that should be credential references
        if _key_is_sensitive(k) or any(
            frag in k.upper()
            for frag in (
                "API_KEY",
                "SECRET",
                "PASSWORD",
                "PASSWD",
                "AUTH_TOKEN",
                "ACCESS_TOKEN",
                "PRIVATE_KEY",
            )
        ):
            raise RawSecretStorageError(
                f"Sensitive variable '{k}' must not be stored in environment_variables. "
                "Use credential_refs with a validated broker source reference instead."
            )

        v = str(raw_v)
        # Prohibit values resembling raw tokens / secrets
        if contains_raw_secret_value(v):
            raise RawSecretStorageError(
                f"Variable '{k}' contains a value matching a raw secret format (e.g. API key, token, or private key). "
                "Raw secret values cannot be stored in environment_variables; use credential_refs instead."
            )

        validated[k] = v

    return validated


def validate_profile_version_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Validate all components of an environment profile version specification."""
    if not isinstance(spec, dict):
        raise ValueError("Profile version spec must be a dictionary")

    runtime_profile = str(spec.get("runtime_profile") or "default").strip()
    if not runtime_profile:
        runtime_profile = "default"

    raw_env = spec.get("environment_variables") or {}
    env_vars = validate_environment_variables(raw_env)

    raw_creds = spec.get("credential_refs") or []
    credential_refs = validate_credential_refs(raw_creds)

    packages = spec.get("packages") or {}
    if not isinstance(packages, (dict, list)):
        raise ValueError("packages must be a dictionary or list")

    settings = spec.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("settings must be a dictionary")

    # Prohibit raw secret values nested in settings
    for _sk, _sv in settings.items():
        if isinstance(_sv, str) and contains_raw_secret_value(_sv):
            raise RawSecretStorageError(
                f"settings['{_sk}'] contains a value matching a raw secret format. "
                "Raw secret values must not be stored in settings; use credential_refs instead."
            )

    return {
        "runtime_profile": runtime_profile,
        "environment_variables": env_vars,
        "packages": packages,
        "settings": settings,
        "credential_refs": credential_refs,
    }


class EnvironmentProfileService:
    """Authoritative service for managing Environment Profiles and immutable Versions."""

    @staticmethod
    async def create_profile(
        db: AsyncSession,
        *,
        user_id: str,
        name: str,
        description: str | None = None,
        workspace_id: str | None = None,
        initial_spec: dict[str, Any] | None = None,
    ) -> tuple[EnvironmentProfile, EnvironmentProfileVersion | None]:
        """Create a new EnvironmentProfile, optionally with an initial version."""
        norm_name = str(name or "").strip()
        if not norm_name:
            raise ValueError("Profile name must not be blank")

        user_id = str(user_id or "").strip()
        if not user_id:
            raise ValueError("user_id must not be blank")

        now_ms = int(time.time() * 1000)
        profile = EnvironmentProfile(
            user_id=user_id,
            workspace_id=str(workspace_id).strip() if workspace_id else None,
            name=norm_name,
            description=str(description).strip() if description else None,
            active_version_id=None,
            is_archived=False,
            created_at_ms=now_ms,
            updated_at_ms=now_ms,
        )
        db.add(profile)
        await db.flush()

        initial_version: EnvironmentProfileVersion | None = None
        if initial_spec is not None:
            initial_version = await EnvironmentProfileService.create_version(
                db,
                profile_id=profile.id,
                spec=initial_spec,
                created_by=user_id,
                make_active=True,
            )

        return profile, initial_version

    @staticmethod
    async def create_version(
        db: AsyncSession,
        *,
        profile_id: str,
        spec: dict[str, Any],
        created_by: str | None = None,
        make_active: bool = True,
    ) -> EnvironmentProfileVersion:
        """Create a new immutable version for an existing EnvironmentProfile."""
        profile = await EnvironmentProfileService.get_profile(db, profile_id)
        if profile is None:
            raise EnvironmentProfileNotFoundError(f"EnvironmentProfile '{profile_id}' not found")

        validated = validate_profile_version_spec(spec)
        digest = compute_version_digest(
            runtime_profile=validated["runtime_profile"],
            environment_variables=validated["environment_variables"],
            packages=validated["packages"],
            settings=validated["settings"],
            credential_refs=validated["credential_refs"],
        )

        # Determine next version number
        stmt = (
            select(EnvironmentProfileVersion.version_number)
            .where(EnvironmentProfileVersion.profile_id == profile_id)
            .order_by(EnvironmentProfileVersion.version_number.desc())
            .limit(1)
        )
        res = await db.execute(stmt)
        latest_num = res.scalar_one_or_none() or 0
        next_num = latest_num + 1

        parent_version_id = profile.active_version_id
        now_ms = int(time.time() * 1000)

        version = EnvironmentProfileVersion(
            profile_id=profile_id,
            version_number=next_num,
            digest=digest,
            runtime_profile=validated["runtime_profile"],
            environment_variables=validated["environment_variables"],
            packages=validated["packages"],
            settings=validated["settings"],
            credential_refs=validated["credential_refs"],
            parent_version_id=parent_version_id,
            created_by=str(created_by).strip() if created_by else None,
            created_at_ms=now_ms,
        )
        db.add(version)
        await db.flush()

        if make_active:
            profile.active_version_id = version.id
            profile.updated_at_ms = now_ms
            await db.flush()

        return version

    @staticmethod
    async def get_profile(db: AsyncSession, profile_id: str) -> EnvironmentProfile | None:
        """Get profile by ID."""
        stmt = select(EnvironmentProfile).where(EnvironmentProfile.id == profile_id)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def get_profile_by_name(
        db: AsyncSession, *, user_id: str, name: str
    ) -> EnvironmentProfile | None:
        """Get profile by user_id and name."""
        stmt = select(EnvironmentProfile).where(
            EnvironmentProfile.user_id == user_id,
            EnvironmentProfile.name == name,
        )
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def list_profiles(
        db: AsyncSession,
        *,
        user_id: str,
        workspace_id: str | None = None,
        include_archived: bool = False,
    ) -> list[EnvironmentProfile]:
        """List profiles for a user, optionally filtered by workspace."""
        conditions = [EnvironmentProfile.user_id == user_id]
        if not include_archived:
            conditions.append(EnvironmentProfile.is_archived.is_(False))
        if workspace_id is not None:
            conditions.append(EnvironmentProfile.workspace_id == workspace_id)

        stmt = (
            select(EnvironmentProfile).where(*conditions).order_by(EnvironmentProfile.created_at_ms)
        )
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def get_version(db: AsyncSession, version_id: str) -> EnvironmentProfileVersion | None:
        """Get profile version by ID."""
        stmt = select(EnvironmentProfileVersion).where(EnvironmentProfileVersion.id == version_id)
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    @staticmethod
    async def list_versions(db: AsyncSession, profile_id: str) -> list[EnvironmentProfileVersion]:
        """List all versions for a profile in chronological version order."""
        stmt = (
            select(EnvironmentProfileVersion)
            .where(EnvironmentProfileVersion.profile_id == profile_id)
            .order_by(EnvironmentProfileVersion.version_number)
        )
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def set_active_version(
        db: AsyncSession, *, profile_id: str, version_id: str
    ) -> EnvironmentProfile:
        """Switch active version of a profile."""
        profile = await EnvironmentProfileService.get_profile(db, profile_id)
        if profile is None:
            raise EnvironmentProfileNotFoundError(f"EnvironmentProfile '{profile_id}' not found")

        version = await EnvironmentProfileService.get_version(db, version_id)
        if version is None or version.profile_id != profile_id:
            raise EnvironmentProfileVersionNotFoundError(
                f"Version '{version_id}' not found for profile '{profile_id}'"
            )

        profile.active_version_id = version_id
        profile.updated_at_ms = int(time.time() * 1000)
        await db.flush()
        return profile

    @staticmethod
    async def archive_profile(db: AsyncSession, profile_id: str) -> EnvironmentProfile:
        """Archive a profile."""
        profile = await EnvironmentProfileService.get_profile(db, profile_id)
        if profile is None:
            raise EnvironmentProfileNotFoundError(f"EnvironmentProfile '{profile_id}' not found")

        profile.is_archived = True
        profile.updated_at_ms = int(time.time() * 1000)
        await db.flush()
        return profile

    @staticmethod
    async def set_profile_target(
        db: "AsyncSession",
        *,
        profile_id: str,
        target_name: str | None,
        user_id: str,
    ) -> "EnvironmentProfile":
        """Set or clear the environment target on a profile.

        Setting target_name to None un-anchors the profile.  Setting it to a
        non-blank string validates the name against the target registry before
        persisting, so an unknown name raises TargetNotFoundError immediately.
        """
        profile = await EnvironmentProfileService.get_profile(db, profile_id)
        if profile is None:
            raise EnvironmentProfileNotFoundError(f"EnvironmentProfile '{profile_id}' not found")

        if profile.user_id != user_id:
            raise TargetOwnershipError(f"User '{user_id}' does not own profile '{profile_id}'")

        norm_target = str(target_name or "").strip() or None

        if norm_target is not None:
            # Validate existence; ownership enforcement happens at resolve time.
            target_registry.get(norm_target)

        profile.target_name = norm_target
        profile.updated_at_ms = int(time.time() * 1000)
        await db.flush()
        return profile

    @staticmethod
    async def resolve_profile_target(
        db: "AsyncSession",
        *,
        profile_id: str,
        caller_user_id: str,
        target_name_override: str | None = None,
    ) -> "EnvironmentTarget":
        """Resolve the active environment target for a profile.

        Loads the profile and its active version, then calls ``resolve_target``
        with the effective target name and runtime_profile.

        Args:
            profile_id: The EnvironmentProfile.id to resolve.
            caller_user_id: Authenticated caller; used for ownership checks on
                restricted targets.
            target_name_override: If provided, use this target name instead of
                the profile's stored ``target_name``.  Still validates against
                the registry.

        Raises:
            EnvironmentProfileNotFoundError: Profile not found.
            EnvironmentProfileVersionNotFoundError: Profile has no active version.
            TargetUnanchoredError: No target name available (profile unanchored
                and no override given).
            TargetNotFoundError: Resolved name is not registered.
            TargetOwnershipError: Caller does not own the profile for a
                restricted target.
            EnvironmentTargetError: Active version's runtime_profile is not
                accepted by the resolved target.
        """

        profile = await EnvironmentProfileService.get_profile(db, profile_id)
        if profile is None:
            raise EnvironmentProfileNotFoundError(f"EnvironmentProfile '{profile_id}' not found")

        if caller_user_id and profile.user_id != caller_user_id:
            raise TargetOwnershipError(
                f"Caller user '{caller_user_id}' does not own profile '{profile_id}'"
            )

        active_version = None
        if profile.active_version_id:
            active_version = await EnvironmentProfileService.get_version(
                db, profile.active_version_id
            )
        if active_version is None:
            raise EnvironmentProfileVersionNotFoundError(
                f"Profile '{profile_id}' has no active version; "
                "publish a version before resolving a target"
            )

        effective_target_name = (
            str(target_name_override or "").strip()
            or str(getattr(profile, "target_name", None) or "").strip()
            or None
        )

        return resolve_target(
            effective_target_name,
            runtime_profile=active_version.runtime_profile,
            owner_user_id=caller_user_id,
            profile_user_id=profile.user_id,
        )
