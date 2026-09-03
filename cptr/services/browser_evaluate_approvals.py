"""Short-lived one-time approvals for user-Chrome Runtime.evaluate commands."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

APPROVAL_TTL_SECONDS = 120.0
MAX_APPROVALS = 512


def _digest(expression: str) -> str:
    return hashlib.sha256(expression.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EvaluateApproval:
    token: str
    user_id: str
    session_id: str
    expression_hash: str
    expires_at: float


class BrowserEvaluateApprovals:
    def __init__(self) -> None:
        self._items: dict[str, EvaluateApproval] = {}

    def issue(self, *, user_id: str, session_id: str, expression: str) -> EvaluateApproval:
        self._prune()
        if len(expression) == 0 or len(expression) > 20_000:
            raise ValueError("evaluate expression must be between 1 and 20000 characters")
        token = secrets.token_urlsafe(32)
        approval = EvaluateApproval(
            token=token,
            user_id=user_id,
            session_id=session_id,
            expression_hash=_digest(expression),
            expires_at=time.monotonic() + APPROVAL_TTL_SECONDS,
        )
        if len(self._items) >= MAX_APPROVALS:
            oldest = min(self._items.values(), key=lambda item: item.expires_at)
            self._items.pop(oldest.token, None)
        self._items[token] = approval
        return approval

    def consume(self, *, token: str, user_id: str, session_id: str, expression: str) -> bool:
        self._prune()
        approval = self._items.pop(token, None)
        if approval is None:
            return False
        return (
            approval.user_id == user_id
            and approval.session_id == session_id
            and approval.expression_hash == _digest(expression)
            and approval.expires_at > time.monotonic()
        )

    def _prune(self) -> None:
        now = time.monotonic()
        expired = [token for token, item in self._items.items() if item.expires_at <= now]
        for token in expired:
            self._items.pop(token, None)


browser_evaluate_approvals = BrowserEvaluateApprovals()
