"""Machine-enforced Dark Factory state and transition authority."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FactoryState(str, Enum):
    """Durable factory run/cycle states.

    ``str, Enum`` is intentional because CPTR supports Python 3.10, where
    ``enum.StrEnum`` is not available.
    """

    MISSION = "MISSION"
    RECOVERING = "RECOVERING"
    BASELINING = "BASELINING"
    UNDERSTANDING = "UNDERSTANDING"
    AUDITING = "AUDITING"
    SELECTING_FINDING = "SELECTING_FINDING"
    CAPABILITY_ANALYSIS = "CAPABILITY_ANALYSIS"
    SKILL_DISCOVERY = "SKILL_DISCOVERY"
    TRUST_EVALUATION = "TRUST_EVALUATION"
    SKILL_SELECTION = "SKILL_SELECTION"
    REPRODUCING = "REPRODUCING"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    TARGETED_VERIFYING = "TARGETED_VERIFYING"
    FULL_VERIFYING = "FULL_VERIFYING"
    ADVERSARIAL_REVIEW = "ADVERSARIAL_REVIEW"
    SECURITY_REVIEW = "SECURITY_REVIEW"
    LIVE_VERIFYING = "LIVE_VERIFYING"
    VICTORY_JUDGING = "VICTORY_JUDGING"
    REPAIR_REQUIRED = "REPAIR_REQUIRED"
    COMMITTING = "COMMITTING"
    PUSHING = "PUSHING"
    CI_VERIFYING = "CI_VERIFYING"
    CYCLE_COMPLETE = "CYCLE_COMPLETE"
    PAUSED = "PAUSED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"


class FactoryActor(str, Enum):
    SYSTEM = "SYSTEM"
    USER = "USER"
    REASONING_ROLE = "REASONING_ROLE"
    WORKER = "WORKER"
    VERIFIER = "VERIFIER"
    CI = "CI"


@dataclass(frozen=True)
class FactoryTransition:
    from_state: FactoryState
    to_state: FactoryState
    actor: FactoryActor
    reason: str
    evidence_ids: tuple[str, ...] = ()


class InvalidFactoryTransition(ValueError):
    """Raised when a state edge or actor authority check fails closed."""


_TERMINAL_STATES = {
    FactoryState.BLOCKED,
    FactoryState.FAILED,
    FactoryState.COMPLETE,
    FactoryState.CANCELLED,
}

_FORWARD_CHAIN = (
    FactoryState.MISSION,
    FactoryState.RECOVERING,
    FactoryState.BASELINING,
    FactoryState.UNDERSTANDING,
    FactoryState.AUDITING,
    FactoryState.SELECTING_FINDING,
    FactoryState.CAPABILITY_ANALYSIS,
    FactoryState.SKILL_DISCOVERY,
    FactoryState.TRUST_EVALUATION,
    FactoryState.SKILL_SELECTION,
    FactoryState.REPRODUCING,
    FactoryState.ROOT_CAUSE_ANALYSIS,
    FactoryState.PLANNING,
    FactoryState.IMPLEMENTING,
    FactoryState.TARGETED_VERIFYING,
    FactoryState.FULL_VERIFYING,
    FactoryState.ADVERSARIAL_REVIEW,
    FactoryState.SECURITY_REVIEW,
    FactoryState.LIVE_VERIFYING,
    FactoryState.VICTORY_JUDGING,
)

_ALLOWED: dict[FactoryState, set[FactoryState]] = {
    current: {target} for current, target in zip(_FORWARD_CHAIN, _FORWARD_CHAIN[1:])
}
_ALLOWED.update(
    {
        FactoryState.VICTORY_JUDGING: {
            FactoryState.COMMITTING,
            FactoryState.REPAIR_REQUIRED,
        },
        FactoryState.COMMITTING: {FactoryState.PUSHING},
        FactoryState.PUSHING: {FactoryState.CI_VERIFYING},
        FactoryState.CI_VERIFYING: {
            FactoryState.CYCLE_COMPLETE,
            FactoryState.REPAIR_REQUIRED,
        },
        FactoryState.CYCLE_COMPLETE: {
            FactoryState.AUDITING,
            FactoryState.COMPLETE,
        },
        FactoryState.REPAIR_REQUIRED: {
            FactoryState.ROOT_CAUSE_ANALYSIS,
            FactoryState.CAPABILITY_ANALYSIS,
            FactoryState.IMPLEMENTING,
        },
    }
)

for _repair_source in (
    FactoryState.TARGETED_VERIFYING,
    FactoryState.FULL_VERIFYING,
    FactoryState.ADVERSARIAL_REVIEW,
    FactoryState.SECURITY_REVIEW,
    FactoryState.LIVE_VERIFYING,
):
    _ALLOWED.setdefault(_repair_source, set()).add(FactoryState.REPAIR_REQUIRED)


_SUCCESS_AUTHORITY_STATES = {
    FactoryState.COMMITTING,
    FactoryState.CYCLE_COMPLETE,
    FactoryState.COMPLETE,
}


def is_terminal_factory_state(state: FactoryState) -> bool:
    return state in _TERMINAL_STATES


def validate_factory_transition(
    from_state: FactoryState,
    to_state: FactoryState,
    actor: FactoryActor,
    *,
    resumable_state: FactoryState | None = None,
    machine_victory: bool = False,
) -> None:
    """Validate one factory transition without performing side effects.

    The state machine is server-authoritative. Model/worker contexts can submit
    evidence and advice but cannot claim success states. Machine Victory is a
    separate precondition for the post-judge commit edge.
    """

    if is_terminal_factory_state(from_state):
        raise InvalidFactoryTransition(f"terminal state {from_state.value} cannot transition")

    if to_state in _SUCCESS_AUTHORITY_STATES and actor is not FactoryActor.SYSTEM:
        raise InvalidFactoryTransition(
            f"actor {actor.value} lacks authority for {to_state.value}"
        )

    if to_state in {FactoryState.BLOCKED, FactoryState.FAILED} and actor is not FactoryActor.SYSTEM:
        raise InvalidFactoryTransition(
            f"actor {actor.value} lacks authority for {to_state.value}"
        )

    if to_state is FactoryState.RECOVERING:
        if actor is not FactoryActor.SYSTEM:
            raise InvalidFactoryTransition(
                f"actor {actor.value} lacks authority for {to_state.value}"
            )
        if from_state in {
            FactoryState.RECOVERING,
            FactoryState.PAUSED,
            FactoryState.APPROVAL_REQUIRED,
        }:
            raise InvalidFactoryTransition(f"cannot recover from {from_state.value}")
        return

    if to_state is FactoryState.CANCELLED:
        if actor not in {FactoryActor.SYSTEM, FactoryActor.USER}:
            raise InvalidFactoryTransition(
                f"actor {actor.value} lacks authority for {to_state.value}"
            )
        return

    if to_state is FactoryState.PAUSED:
        if actor not in {FactoryActor.SYSTEM, FactoryActor.USER}:
            raise InvalidFactoryTransition(
                f"actor {actor.value} lacks authority for {to_state.value}"
            )
        if from_state in {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}:
            raise InvalidFactoryTransition(f"cannot pause from {from_state.value}")
        return

    if to_state is FactoryState.APPROVAL_REQUIRED:
        if actor is not FactoryActor.SYSTEM:
            raise InvalidFactoryTransition(
                f"actor {actor.value} lacks authority for {to_state.value}"
            )
        if from_state in {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}:
            raise InvalidFactoryTransition(
                f"cannot request approval from {from_state.value}"
            )
        return

    if (
        from_state is FactoryState.APPROVAL_REQUIRED
        and to_state is FactoryState.BLOCKED
    ):
        return

    if from_state in {FactoryState.PAUSED, FactoryState.APPROVAL_REQUIRED}:
        if actor not in {FactoryActor.SYSTEM, FactoryActor.USER}:
            raise InvalidFactoryTransition(
                f"actor {actor.value} lacks authority to resume {from_state.value}"
            )
        if resumable_state is None or to_state is not resumable_state:
            expected = resumable_state.value if resumable_state else "<missing>"
            raise InvalidFactoryTransition(
                f"transition must resume the recorded resumable state {expected}"
            )
        return

    if from_state is FactoryState.RECOVERING and resumable_state is not None:
        if actor is not FactoryActor.SYSTEM:
            raise InvalidFactoryTransition(
                f"actor {actor.value} lacks authority to resume {from_state.value}"
            )
        if to_state is not resumable_state:
            raise InvalidFactoryTransition(
                "transition must resume the recorded resumable state "
                f"{resumable_state.value}"
            )
        return

    if to_state in {FactoryState.BLOCKED, FactoryState.FAILED}:
        return

    if (
        from_state is FactoryState.VICTORY_JUDGING
        and to_state is FactoryState.COMMITTING
        and not machine_victory
    ):
        raise InvalidFactoryTransition(
            "VICTORY_JUDGING -> COMMITTING requires machine Victory evidence"
        )

    if to_state not in _ALLOWED.get(from_state, set()):
        raise InvalidFactoryTransition(
            f"invalid factory transition {from_state.value} -> {to_state.value}"
        )
