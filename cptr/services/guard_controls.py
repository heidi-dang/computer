"""Server-authoritative owner-scoped approval Guard Controls.

The registry is code-owned. Persistent rows contain only owner overrides for
mutable approval friction. Locked invariants are never represented as writable
rows and are always reported/enforced as enabled.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from cptr.models.guard_controls import GuardSetting, GuardSettingEvent
from cptr.utils.db import get_db

GuardCategory = Literal["approval", "invariant"]
GuardRisk = Literal["medium", "high", "critical"]


@dataclass(frozen=True, slots=True)
class GuardDefinition:
    id: str
    label: str
    description: str
    category: GuardCategory
    risk: GuardRisk
    mutable: bool
    default_enabled: bool = True


_MUTABLE: tuple[GuardDefinition, ...] = (
    GuardDefinition(
        "delegation_prompt_approval",
        "Delegation approval",
        "Require allow:delegate before ChatGPT can use delegated CPTR/model execution.",
        "approval",
        "high",
        True,
    ),
    GuardDefinition(
        "secret_write_prompt_approval",
        "Secret-write approval",
        "Require allow:secret-write before secret material can be written.",
        "approval",
        "critical",
        True,
    ),
    GuardDefinition(
        "root_prompt_approval",
        "Root prompt approval",
        "Require explicit prompt authorization before requesting a Workbench local-root grant.",
        "approval",
        "critical",
        True,
    ),
    GuardDefinition(
        "network_operation_approval",
        "Network approval",
        "Require per-operation network opt-in before external command or managed-browser traffic.",
        "approval",
        "high",
        True,
    ),
    GuardDefinition(
        "package_install_approval",
        "Package install approval",
        "Require an explicit package-install opt-in before package manager installation commands.",
        "approval",
        "high",
        True,
    ),
    GuardDefinition(
        "browser_evaluate_approval",
        "Browser evaluate approval",
        "Require an expression-bound single-use approval before paired Chrome JavaScript evaluation.",
        "approval",
        "high",
        True,
    ),
    GuardDefinition(
        "autonomous_destructive_approval",
        "Autonomous destructive approval",
        "Pause autonomous work for owner approval before classified external or destructive assignments.",
        "approval",
        "critical",
        True,
    ),
    GuardDefinition(
        "task_review_approval",
        "Task review approval",
        "Require explicit diff review before user-started delegated task changes are accepted.",
        "approval",
        "high",
        True,
    ),
    GuardDefinition(
        "capability_os_external_approval",
        "Capability OS external approval",
        "Require verified explicit approval for authority-critical permissions already covered by standing policy.",
        "approval",
        "critical",
        True,
    ),
    GuardDefinition(
        "destructive_shell_approval",
        "Destructive shell approval",
        "Block commands matched by the destructive-command classifier unless separately authorized.",
        "approval",
        "critical",
        True,
    ),
)

_LOCKED: tuple[GuardDefinition, ...] = (
    GuardDefinition("identity_authentication", "Identity authentication", "Authenticated identity is always required.", "invariant", "critical", False),
    GuardDefinition("control_api_scopes", "Control API scopes", "Scoped bearer authority is always intersected with requested operations.", "invariant", "critical", False),
    GuardDefinition("owner_workspace_isolation", "Owner/workspace isolation", "Resources remain owner and workspace scoped.", "invariant", "critical", False),
    GuardDefinition("workspace_path_confinement", "Workspace path confinement", "Workspace file operations remain traversal-confined.", "invariant", "critical", False),
    GuardDefinition("stale_sha_protection", "Stale SHA protection", "Optimistic file preconditions remain enforced.", "invariant", "high", False),
    GuardDefinition("browser_lease_epoch_validation", "Browser lease epoch validation", "Paired Chrome mutations require current lease ownership and epoch.", "invariant", "critical", False),
    GuardDefinition("secret_output_redaction", "Secret output redaction", "Secret values remain excluded from public results and telemetry.", "invariant", "critical", False),
    GuardDefinition("dedicated_ssh_boundary", "Dedicated SSH boundary", "Raw SSH transport remains restricted to the dedicated SSH control path.", "invariant", "critical", False),
    GuardDefinition("live_ticket_signing_revocation", "Live-ticket signing/revocation", "Workbench live capabilities remain signed, generation-bound and revocable.", "invariant", "high", False),
    GuardDefinition("capability_lease_digest_validation", "Capability lease/digest validation", "Capability OS leases remain task, artifact and permission bound.", "invariant", "critical", False),
    GuardDefinition("factory_verification_integrity", "Factory verification integrity", "Verification and Victory evidence remain server authoritative.", "invariant", "critical", False),
    GuardDefinition("cors_origin_validation", "CORS/origin validation", "Configured public and browser origins remain validated.", "invariant", "high", False),
)

GUARD_DEFINITIONS: tuple[GuardDefinition, ...] = _MUTABLE + _LOCKED
GUARD_REGISTRY: dict[str, GuardDefinition] = {item.id: item for item in GUARD_DEFINITIONS}
MUTABLE_GUARD_IDS: tuple[str, ...] = tuple(item.id for item in _MUTABLE)


class GuardControlError(RuntimeError):
    pass


class GuardNotFoundError(GuardControlError):
    pass


class GuardImmutableError(GuardControlError):
    pass


class GuardVersionConflict(GuardControlError):
    def __init__(self, current: dict):
        super().__init__("guard setting changed; refresh and retry")
        self.current = current


def _definition(guard_id: str) -> GuardDefinition:
    definition = GUARD_REGISTRY.get(str(guard_id).strip())
    if definition is None:
        raise GuardNotFoundError("guard not found")
    return definition


def _public(definition: GuardDefinition, *, enabled: bool, version: int) -> dict:
    return {
        "id": definition.id,
        "label": definition.label,
        "description": definition.description,
        "category": definition.category,
        "risk": definition.risk,
        "mutable": definition.mutable,
        "default_enabled": definition.default_enabled,
        "enabled": bool(enabled),
        "version": int(version),
    }


class GuardPolicyService:
    """Resolve and mutate owner Guard Controls without widening other authority."""

    async def catalog(self, user_id: str) -> dict:
        async with await get_db() as db:
            rows = list(
                (
                    await db.execute(
                        select(GuardSetting).where(GuardSetting.user_id == user_id)
                    )
                )
                .scalars()
                .all()
            )
        overrides = {row.guard_id: row for row in rows if row.guard_id in GUARD_REGISTRY}
        guards = []
        for definition in GUARD_DEFINITIONS:
            row = overrides.get(definition.id) if definition.mutable else None
            guards.append(
                _public(
                    definition,
                    enabled=bool(row.enabled) if row is not None else definition.default_enabled,
                    version=int(row.version) if row is not None else 0,
                )
            )
        return {
            "guards": guards,
            "mutable_count": len(_MUTABLE),
            "enabled_mutable_count": sum(
                1 for item in guards if item["mutable"] and item["enabled"]
            ),
            "locked_count": len(_LOCKED),
        }

    async def current(self, user_id: str, guard_id: str) -> dict:
        definition = _definition(guard_id)
        if not definition.mutable:
            return _public(definition, enabled=True, version=0)
        async with await get_db() as db:
            row = await db.get(GuardSetting, (user_id, definition.id))
        return _public(
            definition,
            enabled=bool(row.enabled) if row is not None else definition.default_enabled,
            version=int(row.version) if row is not None else 0,
        )

    async def is_enabled(self, user_id: str, guard_id: str) -> bool:
        definition = _definition(guard_id)
        if not definition.mutable:
            return True
        try:
            return bool((await self.current(user_id, guard_id))["enabled"])
        except SQLAlchemyError:
            # Execution gates fail closed if policy persistence is unavailable or
            # a rolling deployment has not applied the Guard Controls migration yet.
            return True

    async def set_guard(
        self,
        user_id: str,
        guard_id: str,
        *,
        enabled: bool,
        expected_version: int,
        source: str = "mcp_ui",
        now_ms: int | None = None,
    ) -> dict:
        definition = _definition(guard_id)
        if not definition.mutable:
            raise GuardImmutableError("locked security invariants cannot be changed")
        if expected_version < 0:
            raise ValueError("expected_version must be non-negative")
        timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
        normalized_source = str(source).strip()[:64] or "unknown"

        async with await get_db() as db:
            row = await db.get(GuardSetting, (user_id, definition.id))
            current_enabled = (
                bool(row.enabled) if row is not None else definition.default_enabled
            )
            current_version = int(row.version) if row is not None else 0
            if current_version != expected_version:
                raise GuardVersionConflict(
                    _public(definition, enabled=current_enabled, version=current_version)
                )

            next_version = current_version + 1
            if row is None:
                db.add(
                    GuardSetting(
                        user_id=user_id,
                        guard_id=definition.id,
                        enabled=bool(enabled),
                        version=next_version,
                        updated_at=timestamp,
                    )
                )
            else:
                result = await db.execute(
                    update(GuardSetting)
                    .where(
                        GuardSetting.user_id == user_id,
                        GuardSetting.guard_id == definition.id,
                        GuardSetting.version == expected_version,
                    )
                    .values(
                        enabled=bool(enabled),
                        version=next_version,
                        updated_at=timestamp,
                    )
                )
                if result.rowcount != 1:
                    await db.rollback()
                    raise GuardVersionConflict(await self.current(user_id, definition.id))

            db.add(
                GuardSettingEvent(
                    user_id=user_id,
                    guard_id=definition.id,
                    previous_enabled=current_enabled,
                    new_enabled=bool(enabled),
                    version=next_version,
                    source=normalized_source,
                    created_at=timestamp,
                )
            )
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise GuardVersionConflict(await self.current(user_id, definition.id)) from exc

        return _public(definition, enabled=bool(enabled), version=next_version)

    async def reset(
        self,
        user_id: str,
        *,
        expected_versions: dict[str, int],
        source: str = "mcp_ui",
        now_ms: int | None = None,
    ) -> dict:
        timestamp = int(time.time() * 1000) if now_ms is None else int(now_ms)
        for guard_id, expected_version in expected_versions.items():
            definition = _definition(guard_id)
            if not definition.mutable:
                raise GuardImmutableError("locked security invariants cannot be reset")
            await self.set_guard(
                user_id,
                guard_id,
                enabled=definition.default_enabled,
                expected_version=int(expected_version),
                source=source,
                now_ms=timestamp,
            )
        return await self.catalog(user_id)


guard_policy_service = GuardPolicyService()
