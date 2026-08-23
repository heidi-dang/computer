"""Bounded FlowDeck run budgets."""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceeded(RuntimeError):
    """Raised when a run-level budget would be exceeded."""


@dataclass
class RunBudget:
    max_steps: int
    max_attempts: int
    max_delegations: int
    max_tool_calls: int
    max_model_turns: int
    max_wall_seconds: int
    steps: int = 0
    attempts: int = 0
    delegations: int = 0
    tool_calls: int = 0
    model_turns: int = 0

    def _consume(self, field: str, limit: int, amount: int = 1) -> None:
        current = getattr(self, field)
        if amount < 0 or current + amount > limit:
            raise BudgetExceeded(f"{field} budget exceeded")
        setattr(self, field, current + amount)

    def consume_step(self) -> None:
        self._consume("steps", self.max_steps)

    def consume_attempt(self) -> None:
        self._consume("attempts", self.max_attempts)

    def consume_delegation(self) -> None:
        self._consume("delegations", self.max_delegations)

    def consume_tool_call(self) -> None:
        self._consume("tool_calls", self.max_tool_calls)

    def consume_model_turn(self) -> None:
        self._consume("model_turns", self.max_model_turns)

    def validate_wall_time(self, elapsed_seconds: float) -> None:
        if elapsed_seconds < 0 or elapsed_seconds > self.max_wall_seconds:
            raise BudgetExceeded("wall-clock budget exceeded")