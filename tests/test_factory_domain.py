import unittest

from cptr.services.factory_domain import (
    FactoryActor,
    FactoryState,
    InvalidFactoryTransition,
    is_terminal_factory_state,
    validate_factory_transition,
)


class FactoryDomainTests(unittest.TestCase):
    def test_state_enum_covers_the_architecture_contract(self):
        self.assertEqual(
            {state.value for state in FactoryState},
            {
                "MISSION",
                "RECOVERING",
                "BASELINING",
                "UNDERSTANDING",
                "AUDITING",
                "SELECTING_FINDING",
                "CAPABILITY_ANALYSIS",
                "SKILL_DISCOVERY",
                "TRUST_EVALUATION",
                "SKILL_SELECTION",
                "REPRODUCING",
                "ROOT_CAUSE_ANALYSIS",
                "PLANNING",
                "IMPLEMENTING",
                "TARGETED_VERIFYING",
                "FULL_VERIFYING",
                "ADVERSARIAL_REVIEW",
                "SECURITY_REVIEW",
                "LIVE_VERIFYING",
                "VICTORY_JUDGING",
                "REPAIR_REQUIRED",
                "COMMITTING",
                "PUSHING",
                "CI_VERIFYING",
                "CYCLE_COMPLETE",
                "PAUSED",
                "APPROVAL_REQUIRED",
                "BLOCKED",
                "FAILED",
                "COMPLETE",
                "CANCELLED",
            },
        )

    def test_normal_forward_chain_is_explicitly_allowed(self):
        chain = [
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
        ]
        for current, target in zip(chain, chain[1:]):
            with self.subTest(current=current, target=target):
                validate_factory_transition(current, target, FactoryActor.SYSTEM)

    def test_victory_path_requires_machine_authority(self):
        with self.assertRaisesRegex(InvalidFactoryTransition, "machine Victory"):
            validate_factory_transition(
                FactoryState.VICTORY_JUDGING,
                FactoryState.COMMITTING,
                FactoryActor.SYSTEM,
            )

        validate_factory_transition(
            FactoryState.VICTORY_JUDGING,
            FactoryState.COMMITTING,
            FactoryActor.SYSTEM,
            machine_victory=True,
        )
        validate_factory_transition(
            FactoryState.COMMITTING,
            FactoryState.PUSHING,
            FactoryActor.SYSTEM,
        )
        validate_factory_transition(
            FactoryState.PUSHING,
            FactoryState.CI_VERIFYING,
            FactoryActor.SYSTEM,
        )
        validate_factory_transition(
            FactoryState.CI_VERIFYING,
            FactoryState.CYCLE_COMPLETE,
            FactoryActor.SYSTEM,
        )
        validate_factory_transition(
            FactoryState.CYCLE_COMPLETE,
            FactoryState.AUDITING,
            FactoryActor.SYSTEM,
        )
        validate_factory_transition(
            FactoryState.CYCLE_COMPLETE,
            FactoryState.COMPLETE,
            FactoryActor.SYSTEM,
        )

    def test_worker_and_reasoning_role_cannot_claim_success(self):
        for actor in (FactoryActor.WORKER, FactoryActor.REASONING_ROLE):
            for target in (FactoryState.COMMITTING, FactoryState.CYCLE_COMPLETE, FactoryState.COMPLETE):
                with self.subTest(actor=actor, target=target):
                    with self.assertRaisesRegex(InvalidFactoryTransition, "authority"):
                        validate_factory_transition(
                            FactoryState.VICTORY_JUDGING,
                            target,
                            actor,
                            machine_victory=True,
                        )

    def test_repair_loop_can_return_to_analysis_or_implementation(self):
        repair_sources = {
            FactoryState.TARGETED_VERIFYING,
            FactoryState.FULL_VERIFYING,
            FactoryState.ADVERSARIAL_REVIEW,
            FactoryState.SECURITY_REVIEW,
            FactoryState.LIVE_VERIFYING,
            FactoryState.VICTORY_JUDGING,
            FactoryState.CI_VERIFYING,
        }
        for source in repair_sources:
            with self.subTest(source=source):
                validate_factory_transition(source, FactoryState.REPAIR_REQUIRED, FactoryActor.SYSTEM)

        for target in (
            FactoryState.ROOT_CAUSE_ANALYSIS,
            FactoryState.CAPABILITY_ANALYSIS,
            FactoryState.IMPLEMENTING,
        ):
            with self.subTest(target=target):
                validate_factory_transition(
                    FactoryState.REPAIR_REQUIRED,
                    target,
                    FactoryActor.SYSTEM,
                )

    def test_pause_and_resume_require_the_recorded_resumable_state(self):
        validate_factory_transition(
            FactoryState.IMPLEMENTING,
            FactoryState.PAUSED,
            FactoryActor.USER,
        )
        validate_factory_transition(
            FactoryState.PAUSED,
            FactoryState.IMPLEMENTING,
            FactoryActor.USER,
            resumable_state=FactoryState.IMPLEMENTING,
        )
        with self.assertRaisesRegex(InvalidFactoryTransition, "resumable"):
            validate_factory_transition(
                FactoryState.PAUSED,
                FactoryState.AUDITING,
                FactoryActor.USER,
                resumable_state=FactoryState.IMPLEMENTING,
            )

    def test_approval_resume_requires_the_recorded_resumable_state(self):
        validate_factory_transition(
            FactoryState.PUSHING,
            FactoryState.APPROVAL_REQUIRED,
            FactoryActor.SYSTEM,
        )
        validate_factory_transition(
            FactoryState.APPROVAL_REQUIRED,
            FactoryState.PUSHING,
            FactoryActor.SYSTEM,
            resumable_state=FactoryState.PUSHING,
        )
        with self.assertRaisesRegex(InvalidFactoryTransition, "resumable"):
            validate_factory_transition(
                FactoryState.APPROVAL_REQUIRED,
                FactoryState.COMMITTING,
                FactoryActor.SYSTEM,
                resumable_state=FactoryState.PUSHING,
            )

    def test_invalid_shortcut_is_rejected(self):
        with self.assertRaises(InvalidFactoryTransition):
            validate_factory_transition(
                FactoryState.MISSION,
                FactoryState.IMPLEMENTING,
                FactoryActor.SYSTEM,
            )

    def test_terminal_states_are_closed(self):
        for state in (FactoryState.BLOCKED, FactoryState.FAILED, FactoryState.COMPLETE, FactoryState.CANCELLED):
            self.assertTrue(is_terminal_factory_state(state))
            with self.assertRaisesRegex(InvalidFactoryTransition, "terminal"):
                validate_factory_transition(state, FactoryState.AUDITING, FactoryActor.SYSTEM)

        self.assertFalse(is_terminal_factory_state(FactoryState.AUDITING))

    def test_only_system_or_user_can_force_terminal_control_states(self):
        validate_factory_transition(
            FactoryState.AUDITING,
            FactoryState.CANCELLED,
            FactoryActor.USER,
        )
        validate_factory_transition(
            FactoryState.AUDITING,
            FactoryState.BLOCKED,
            FactoryActor.SYSTEM,
        )
        validate_factory_transition(
            FactoryState.AUDITING,
            FactoryState.FAILED,
            FactoryActor.SYSTEM,
        )
        for actor in (FactoryActor.WORKER, FactoryActor.REASONING_ROLE, FactoryActor.VERIFIER, FactoryActor.CI):
            with self.subTest(actor=actor):
                with self.assertRaisesRegex(InvalidFactoryTransition, "authority"):
                    validate_factory_transition(
                        FactoryState.AUDITING,
                        FactoryState.FAILED,
                        actor,
                    )


if __name__ == "__main__":
    unittest.main()
