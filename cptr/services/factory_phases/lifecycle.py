"""Git/push/CI phase handlers for the durable Dark Factory state machine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

from cptr.models import FactoryCommitIntent
from cptr.services.factory_ci import FactoryCiService
from cptr.services.factory_domain import FactoryState
from cptr.services.factory_git import FactoryGitService, PushAuthorization
from cptr.services.factory_gates import EvidenceAuthority

from .types import (
    PhaseArtifact,
    PhaseContext,
    PhaseFailure,
    PhaseFailureCategory,
    PhaseOutcome,
)


RepoRootResolver = Callable[[PhaseContext], Awaitable[str]]
PushAuthorizationResolver = Callable[
    [PhaseContext, FactoryCommitIntent], Awaitable[PushAuthorization | None]
]


@dataclass(frozen=True)
class CiTrackingIdentity:
    provider: str
    repository: str
    revision: str
    external_run_id: str
    check_id: str = ""
    url: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("provider", "repository", "revision", "external_run_id"):
            value = str(getattr(self, field_name)).strip()
            if not value:
                raise ValueError(f"CI tracking {field_name} must not be blank")
        if self.check_id and not self.check_id.strip():
            raise ValueError("CI tracking check_id must be blank or non-whitespace")


CiIdentityResolver = Callable[[PhaseContext], Awaitable[CiTrackingIdentity]]


class CommittingPhaseHandler:
    """Prepare/recover one verified commit intent and create the commit."""

    def __init__(
        self,
        *,
        git_service: FactoryGitService,
        repo_root_resolver: RepoRootResolver,
        repository_key: str,
        commit_message: str,
    ) -> None:
        if not repository_key.strip():
            raise ValueError("repository_key must not be blank")
        if not commit_message.strip():
            raise ValueError("commit_message must not be blank")
        self._git = git_service
        self._repo_root = repo_root_resolver
        self._repository_key = repository_key
        self._commit_message = commit_message

    async def execute(self, context: PhaseContext) -> PhaseOutcome:
        repo_root = await self._repo_root(context)
        intent = await self._git.prepare_commit_intent(
            run_id=context.run.id,
            cycle_id=context.cycle.id,
            repo_root=repo_root,
            repository_key=self._repository_key,
            message=self._commit_message,
        )
        committed = await self._git.commit_intent(intent.id, repo_root=repo_root)
        return PhaseOutcome(
            next_state=FactoryState.PUSHING,
            reason="verified factory diff committed through durable commit intent",
            artifacts=(
                PhaseArtifact(
                    key="git-commit",
                    kind="git_commit",
                    source="cptr-git",
                    authority=EvidenceAuthority.MACHINE,
                    revision=committed.commit_sha,
                    fingerprint=context.cycle.target_fingerprint,
                    payload={
                        "commit_intent_id": committed.id,
                        "commit_sha": committed.commit_sha,
                        "diff_digest": committed.diff_digest,
                        "changed_paths": list(committed.changed_paths or []),
                    },
                ),
            ),
        )


class PushingPhaseHandler:
    """Require a revision-bound approval envelope before invoking Git push."""

    def __init__(
        self,
        *,
        git_service: FactoryGitService,
        repo_root_resolver: RepoRootResolver,
        authorization_resolver: PushAuthorizationResolver,
    ) -> None:
        self._git = git_service
        self._repo_root = repo_root_resolver
        self._authorization = authorization_resolver

    async def execute(self, context: PhaseContext) -> PhaseOutcome:
        intent = await self._git.get_intent_for_cycle(context.cycle.id)
        authorization = await self._authorization(context, intent)
        if authorization is None:
            return PhaseOutcome(
                next_state=FactoryState.APPROVAL_REQUIRED,
                reason="push requires an explicit revision-bound approval",
                run_next_action="approve the exact prepared factory commit push envelope",
            )
        repo_root = await self._repo_root(context)
        pushed = await self._git.push_commit(
            intent.id,
            repo_root=repo_root,
            authorization=authorization,
        )
        return PhaseOutcome(
            next_state=FactoryState.CI_VERIFYING,
            reason="approved factory commit pushed successfully",
            artifacts=(
                PhaseArtifact(
                    key="git-push",
                    kind="git_push",
                    source="cptr-git",
                    authority=EvidenceAuthority.MACHINE,
                    revision=pushed.commit_sha,
                    fingerprint=context.cycle.target_fingerprint,
                    payload={
                        "commit_intent_id": pushed.id,
                        "commit_sha": pushed.commit_sha,
                        "remote": pushed.push_remote,
                        "branch": pushed.push_branch,
                        "approval_id": pushed.push_approval_id,
                    },
                ),
            ),
        )


class CiVerifyingPhaseHandler:
    """Observe CI exactly once per call and advance only on terminal success."""

    def __init__(
        self,
        *,
        ci_service: FactoryCiService,
        identity_resolver: CiIdentityResolver,
    ) -> None:
        self._ci = ci_service
        self._identity = identity_resolver

    async def execute(self, context: PhaseContext) -> PhaseOutcome:
        identity = await self._identity(context)
        tracked = await self._ci.begin_tracking(
            run_id=context.run.id,
            cycle_id=context.cycle.id,
            provider=identity.provider,
            repository=identity.repository,
            revision=identity.revision,
            external_run_id=identity.external_run_id,
            check_id=identity.check_id or None,
            url=identity.url,
        )
        observed = await self._ci.poll_once(tracked.id)
        if observed.status != "COMPLETED":
            return PhaseOutcome(
                reason="CI is still running; no terminal conclusion inferred",
                run_next_action=(
                    f"observe CI provider {observed.provider} run {observed.external_run_id} again"
                ),
            )

        artifact = PhaseArtifact(
            key="ci-result",
            kind="ci_result",
            source=f"ci:{observed.provider}",
            authority=EvidenceAuthority.MACHINE,
            revision=observed.revision,
            fingerprint=context.cycle.target_fingerprint,
            payload={
                "provider": observed.provider,
                "repository": observed.repository,
                "external_run_id": observed.external_run_id,
                "check_id": observed.check_id,
                "status": observed.status,
                "conclusion": observed.conclusion,
                "failure_summary": observed.failure_summary,
            },
        )
        if observed.conclusion == "SUCCESS":
            return PhaseOutcome(
                next_state=FactoryState.CYCLE_COMPLETE,
                reason="revision-bound CI completed successfully",
                artifacts=(artifact,),
            )
        conclusion = observed.conclusion or "UNKNOWN"
        return PhaseOutcome(
            reason="terminal CI did not satisfy success policy",
            artifacts=(artifact,),
            failure=PhaseFailure(
                category=PhaseFailureCategory.CI,
                code=(
                    "CI_FAILURE"
                    if conclusion == "FAILURE"
                    else f"CI_{conclusion.replace('-', '_')}"
                ),
                gate_id="ci",
                summary=(
                    observed.failure_summary
                    or f"CI concluded {conclusion} for revision {observed.revision}"
                )[:4_000],
            ),
        )
