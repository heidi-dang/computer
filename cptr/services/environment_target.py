"""Deterministic environment target resolution for EnvironmentProfile.

An EnvironmentTarget is a stable, named deployment context (e.g. "local",
"aws", "production") that a profile version is anchored to.  Resolution is
intentionally strict:

* The name must be non-blank and match a registered target exactly.
* If a profile's ``target_name`` is blank/None the profile is considered
  un-anchored; callers that require a concrete target must pass one explicitly.
* Ambiguity (multiple registrations under the same name) is a programmer error
  and causes TargetRegistryIntegrityError at import-time.
* Missing or unrecognised names raise TargetNotFoundError — never fall through
  silently to a default.

No live deployment actions are performed here; the module provides the
contract and validation layer only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class EnvironmentTargetError(Exception):
    """Base error for environment target resolution."""


class TargetNotFoundError(EnvironmentTargetError):
    """Raised when a requested target name is not registered."""


class TargetAmbiguityError(EnvironmentTargetError):
    """Raised when a target name matches multiple registered entries (registry bug)."""


class TargetOwnershipError(EnvironmentTargetError):
    """Raised when a profile owner does not match the target's ownership constraints."""


class TargetRegistryIntegrityError(EnvironmentTargetError):
    """Raised at registry construction when a duplicate name is detected."""


class TargetUnanchoredError(EnvironmentTargetError):
    """Raised when a profile has no target_name and a concrete target is required."""


# ---------------------------------------------------------------------------
# Target descriptor
# ---------------------------------------------------------------------------

_TARGET_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class EnvironmentTarget:
    """Immutable descriptor for one deployment target environment.

    Attributes:
        name: Stable lowercase identifier (e.g. "local", "aws", "production").
        display_name: Human-readable label.
        description: Informational text; must not contain credential values.
        execution_class: Logical execution class ("local" | "cloud" | "production").
        allowed_runtime_profiles: Frozenset of runtime_profile strings that are
            valid for this target.  Empty frozenset means any profile is allowed.
        requires_explicit_owner: When True, target resolution requires a non-blank
            owner_user_id argument that matches the profile's user_id.
        metadata: Opaque caller-supplied metadata (no secrets, not stored in DB).
    """

    name: str
    display_name: str
    description: str = ""
    execution_class: str = "local"  # "local" | "cloud" | "production"
    allowed_runtime_profiles: frozenset = field(default_factory=frozenset)
    requires_explicit_owner: bool = False
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _TARGET_NAME_RE.match(self.name):
            raise ValueError(
                f"EnvironmentTarget name '{self.name}' must match ^[a-z][a-z0-9_-]{{0,63}}$"
            )
        valid_classes = {"local", "cloud", "production"}
        if self.execution_class not in valid_classes:
            raise ValueError(
                f"EnvironmentTarget execution_class '{self.execution_class}' must be one of {sorted(valid_classes)}"
            )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "execution_class": self.execution_class,
            "allowed_runtime_profiles": sorted(self.allowed_runtime_profiles),
            "requires_explicit_owner": self.requires_explicit_owner,
        }

    def accepts_runtime_profile(self, runtime_profile: str) -> bool:
        """Return True if this target permits the given runtime_profile string."""
        if not self.allowed_runtime_profiles:
            return True  # empty means unrestricted
        return runtime_profile in self.allowed_runtime_profiles


# ---------------------------------------------------------------------------
# Built-in target registry
# ---------------------------------------------------------------------------

_BUILTIN_TARGETS: list = [
    EnvironmentTarget(
        name="local",
        display_name="Local Host",
        description=(
            "Execution directly on the host machine running cptr. "
            "No network dispatch; credentials from local broker only."
        ),
        execution_class="local",
        allowed_runtime_profiles=frozenset({"default", "cptr-vm", "host"}),
        requires_explicit_owner=False,
    ),
    EnvironmentTarget(
        name="aws",
        display_name="AWS Cloud",
        description=(
            "Execution dispatched to an AWS-backed compute environment. "
            "Credential refs must resolve through an authorized cloud broker source."
        ),
        execution_class="cloud",
        allowed_runtime_profiles=frozenset(),  # any runtime profile allowed
        requires_explicit_owner=True,
    ),
    EnvironmentTarget(
        name="production",
        display_name="Production",
        description=(
            "Live production environment. Mutations are real. "
            "Requires explicit owner confirmation and production-approved runtime profiles."
        ),
        execution_class="production",
        allowed_runtime_profiles=frozenset({"production", "cptr-vm-prod"}),
        requires_explicit_owner=True,
    ),
]


class EnvironmentTargetRegistry:
    """In-process registry of known EnvironmentTarget descriptors.

    The registry is intentionally simple: names must be unique, and all lookups
    fail explicitly rather than falling through to a default.
    """

    def __init__(self, targets: list) -> None:
        seen: dict = {}
        for t in targets:
            if t.name in seen:
                raise TargetRegistryIntegrityError(
                    f"Duplicate EnvironmentTarget name '{t.name}' registered — "
                    "each target name must be unique within the registry"
                )
            seen[t.name] = t
        self._targets: dict = seen

    # ------------------------------------------------------------------
    # Query API
    # ------------------------------------------------------------------

    def list_targets(self) -> list:
        """Return all registered targets in stable name order."""
        return sorted(self._targets.values(), key=lambda t: t.name)

    def get(self, name: str) -> EnvironmentTarget:
        """Return the target for *name*, raising TargetNotFoundError if absent."""
        name = str(name or "").strip()
        if not name:
            raise TargetNotFoundError(
                "target name must not be blank; pass an explicit name to resolve"
            )
        target = self._targets.get(name)
        if target is None:
            available = sorted(self._targets)
            raise TargetNotFoundError(
                f"Environment target '{name}' is not registered. Available targets: {available}"
            )
        return target

    def resolve(
        self,
        *,
        target_name: str,
        runtime_profile: str,
        owner_user_id: "str | None" = None,
        profile_user_id: "str | None" = None,
    ) -> EnvironmentTarget:
        """Resolve *target_name* to an EnvironmentTarget, enforcing all contracts.

        Args:
            target_name: Stable target identifier.
            runtime_profile: The profile version's runtime_profile string.
            owner_user_id: Caller-supplied user_id for ownership validation.
                Required when the target has ``requires_explicit_owner=True``.
            profile_user_id: The profile's stored user_id (must match
                ``owner_user_id`` when the target requires explicit ownership).

        Raises:
            TargetNotFoundError: Name is blank or not registered.
            TargetOwnershipError: Ownership check fails.
            EnvironmentTargetError: runtime_profile not accepted by this target.
        """
        target = self.get(target_name)

        if target.requires_explicit_owner:
            owner = str(owner_user_id or "").strip()
            if not owner:
                raise TargetOwnershipError(
                    f"Target '{target_name}' requires an explicit owner_user_id; none was provided"
                )
            prof_owner = str(profile_user_id or "").strip()
            if prof_owner and owner != prof_owner:
                raise TargetOwnershipError(
                    f"Target '{target_name}': caller user_id '{owner}' does not "
                    f"match profile owner '{prof_owner}'"
                )

        norm_runtime = str(runtime_profile or "default").strip() or "default"
        if not target.accepts_runtime_profile(norm_runtime):
            raise EnvironmentTargetError(
                f"Target '{target_name}' does not accept runtime_profile "
                f"'{norm_runtime}'. Allowed profiles: "
                f"{sorted(target.allowed_runtime_profiles) or 'any'}"
            )

        return target


# ---------------------------------------------------------------------------
# Module-level singleton — import-time integrity check
# ---------------------------------------------------------------------------

#: The authoritative runtime registry of EnvironmentTargets.
target_registry: EnvironmentTargetRegistry = EnvironmentTargetRegistry(_BUILTIN_TARGETS)


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------


def resolve_target(
    target_name: "str | None",
    *,
    runtime_profile: str = "default",
    owner_user_id: "str | None" = None,
    profile_user_id: "str | None" = None,
) -> EnvironmentTarget:
    """Resolve a target name, failing explicitly on missing or unanchored input.

    This is the primary entry-point for callers that have a ``target_name``
    from a profile row.  If ``target_name`` is None/blank the call raises
    TargetUnanchoredError rather than silently selecting a default.
    """
    norm = str(target_name or "").strip()
    if not norm:
        raise TargetUnanchoredError(
            "No environment target is set on this profile. "
            "Set target_name to one of the registered targets before resolving: "
            f"{sorted(t.name for t in target_registry.list_targets())}"
        )
    return target_registry.resolve(
        target_name=norm,
        runtime_profile=runtime_profile,
        owner_user_id=owner_user_id,
        profile_user_id=profile_user_id,
    )


__all__ = [
    "EnvironmentTarget",
    "EnvironmentTargetError",
    "EnvironmentTargetRegistry",
    "TargetAmbiguityError",
    "TargetNotFoundError",
    "TargetOwnershipError",
    "TargetRegistryIntegrityError",
    "TargetUnanchoredError",
    "resolve_target",
    "target_registry",
]
