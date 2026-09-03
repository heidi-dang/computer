import json
import unittest

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.services.factory_ci import (
    CiObservation,
    CiPollRequest,
    FactoryCiError,
    FactoryCiService,
    GitHubActionsCliProvider,
)
from cptr.services.factory_git import (
    FactoryGitError,
    FactoryGitService,
    PushAuthorization,
)
from cptr.services.factory_store import SqlFactoryStore


class _FakeGit:
    def __init__(self):
        self.revision = "rev-verified"
        self.fingerprint = "fp-verified"
        self.changed_paths = ["src/app.py"]
        self.diff_payload = {
            "files": [
                {
                    "path": "src/app.py",
                    "hunks": [
                        {
                            "header": "@@ -1 +1 @@",
                            "lines": [
                                {"type": "removed", "content": "old"},
                                {"type": "added", "content": "new"},
                            ],
                        }
                    ],
                }
            ]
        }
        self.diff_check_passed = True
        self.logs = []
        self.stage_calls = []
        self.commit_calls = []
        self.push_calls = []

    async def current_revision(self, _root):
        return self.revision

    async def workspace_fingerprint(self, _root):
        return self.fingerprint

    async def change_manifest(self, _root):
        return [{"status": "modified", "path": path} for path in self.changed_paths]

    async def diff(self, _root):
        return self.diff_payload

    async def diff_check(self, _root):
        return {"passed": self.diff_check_passed, "returncode": 0 if self.diff_check_passed else 1}

    async def stage(self, _root, paths):
        self.stage_calls.append(tuple(paths))

    async def commit(self, _root, message):
        self.commit_calls.append(message)
        self.revision = "commit-sha-1"
        self.logs.insert(0, {"hash": self.revision, "message": message})
        return {"hash": self.revision, "message": message}

    async def log(self, _root, limit=50):
        return self.logs[:limit]

    async def push(self, _root, *, remote, branch):
        self.push_calls.append((remote, branch))
        return {"ok": True, "message": "pushed"}


class _CiProvider:
    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = []

    async def observe(self, request):
        self.calls.append(request)
        return self.observations.pop(0)


class _GhRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def __call__(self, argv, timeout_seconds):
        self.calls.append((argv, timeout_seconds))
        return self.responses.pop(0)


class FactoryGitCiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.store = SqlFactoryStore(session_factory=self.sessions)
        self.run = await self.store.create_run(
            user_id="user-1",
            workspace_id="workspace-1",
            mission="commit and verify a coherent factory cycle",
            acceptance_criteria=["verified changes are committed and CI passes"],
            policy={},
            budget={},
            model_id="configured-model",
            idempotency_key="phase7-git-ci",
        )
        self.cycle = await self.store.create_cycle(
            self.run.id,
            base_revision="rev-base",
            base_fingerprint="fp-base",
            idempotency_key="cycle-1",
        )
        await self.store.set_cycle_target(
            self.run.id,
            self.cycle.id,
            revision="rev-verified",
            fingerprint="fp-verified",
            idempotency_key="target-verified",
        )
        self.git = _FakeGit()
        self.git_service = FactoryGitService(session_factory=self.sessions, git=self.git)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_commit_intent_binds_verified_revision_fingerprint_and_actual_diff(self):
        intent = await self.git_service.prepare_commit_intent(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            repo_root="/repo",
            repository_key=".",
            message="factory: verified cycle",
        )

        self.assertEqual(intent.verified_revision, "rev-verified")
        self.assertEqual(intent.verified_fingerprint, "fp-verified")
        self.assertEqual(intent.changed_paths, ["src/app.py"])
        self.assertEqual(len(intent.diff_digest), 64)
        self.assertEqual(intent.status, "PREPARED")

        committed = await self.git_service.commit_intent(intent.id, repo_root="/repo")

        self.assertEqual(committed.status, "COMMITTED")
        self.assertEqual(committed.commit_sha, "commit-sha-1")
        self.assertEqual(self.git.stage_calls, [("src/app.py",)])
        self.assertEqual(len(self.git.commit_calls), 1)

    async def test_duplicate_commit_recovery_detects_existing_commit_without_committing_again(self):
        intent = await self.git_service.prepare_commit_intent(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            repo_root="/repo",
            repository_key=".",
            message="factory: recover duplicate commit",
        )
        self.git.logs = [{"hash": "commit-after-crash", "message": intent.commit_message}]
        self.git.revision = "commit-after-crash"

        recovered = await self.git_service.commit_intent(intent.id, repo_root="/repo")

        self.assertEqual(recovered.status, "COMMITTED")
        self.assertEqual(recovered.commit_sha, "commit-after-crash")
        self.assertEqual(self.git.commit_calls, [])

    async def test_stale_verified_revision_blocks_commit_intent(self):
        self.git.revision = "rev-mutated-after-verification"

        with self.assertRaises(FactoryGitError) as caught:
            await self.git_service.prepare_commit_intent(
                run_id=self.run.id,
                cycle_id=self.cycle.id,
                repo_root="/repo",
                repository_key=".",
                message="factory: stale commit",
            )

        self.assertEqual(caught.exception.code, "FACTORY_GIT_STALE_VERIFIED_REVISION")
        self.assertEqual(self.git.commit_calls, [])

    async def test_commit_intent_rejects_credential_sensitive_changed_paths(self):
        self.git.changed_paths = ["src/app.py", ".env"]

        with self.assertRaises(FactoryGitError) as caught:
            await self.git_service.prepare_commit_intent(
                run_id=self.run.id,
                cycle_id=self.cycle.id,
                repo_root="/repo",
                repository_key=".",
                message="factory: must not commit secrets",
            )

        self.assertEqual(caught.exception.code, "FACTORY_GIT_SENSITIVE_PATH")
        self.assertEqual(self.git.stage_calls, [])
        self.assertEqual(self.git.commit_calls, [])

    async def test_push_requires_exact_revision_bound_approval_before_provider_invocation(self):
        intent = await self.git_service.prepare_commit_intent(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            repo_root="/repo",
            repository_key=".",
            message="factory: push approval",
        )
        intent = await self.git_service.commit_intent(intent.id, repo_root="/repo")

        with self.assertRaises(FactoryGitError) as caught:
            await self.git_service.push_commit(
                intent.id,
                repo_root="/repo",
                authorization=PushAuthorization(
                    approved=False,
                    approval_id="approval-1",
                    revision=intent.commit_sha,
                    remote="origin",
                    branch="feature",
                ),
            )
        self.assertEqual(caught.exception.code, "FACTORY_GIT_PUSH_APPROVAL_REQUIRED")
        self.assertEqual(self.git.push_calls, [])

        pushed = await self.git_service.push_commit(
            intent.id,
            repo_root="/repo",
            authorization=PushAuthorization(
                approved=True,
                approval_id="approval-1",
                revision=intent.commit_sha,
                remote="origin",
                branch="feature",
            ),
        )
        self.assertEqual(pushed.push_status, "PUSHED")
        self.assertEqual(self.git.push_calls, [("origin", "feature")])

    async def test_ci_failure_requires_diagnosis_before_tracking_a_rerun(self):
        provider = _CiProvider(
            [
                CiObservation(
                    status="COMPLETED",
                    conclusion="FAILURE",
                    failure_summary="unit suite failed",
                )
            ]
        )
        service = FactoryCiService(session_factory=self.sessions, providers={"github": provider})
        tracked = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-1",
            external_run_id="run-1",
            check_id="check-1",
        )

        failed = await service.poll_once(tracked.id)
        self.assertTrue(failed.diagnosis_required)
        self.assertEqual(failed.conclusion, "FAILURE")

        with self.assertRaises(FactoryCiError) as caught:
            await service.begin_tracking(
                run_id=self.run.id,
                cycle_id=self.cycle.id,
                provider="github",
                repository="heidi-dang/computer",
                revision="commit-sha-1",
                external_run_id="run-2",
                check_id="check-2",
            )
        self.assertEqual(caught.exception.code, "FACTORY_CI_DIAGNOSIS_REQUIRED")

        diagnosed = await service.record_diagnosis(failed.id, "failure is in unit:test_widget")
        self.assertFalse(diagnosed.diagnosis_required)
        rerun = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-1",
            external_run_id="run-2",
            check_id="check-2",
        )
        self.assertEqual(rerun.external_run_id, "run-2")

    async def test_latest_ci_failure_invalidates_earlier_success_for_same_revision(self):
        provider = _CiProvider(
            [
                CiObservation(status="COMPLETED", conclusion="SUCCESS"),
                CiObservation(
                    status="COMPLETED",
                    conclusion="FAILURE",
                    failure_summary="later rerun failed",
                ),
            ]
        )
        service = FactoryCiService(session_factory=self.sessions, providers={"github": provider})
        success = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-1",
            external_run_id="run-success",
        )
        failure = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-1",
            external_run_id="run-failure",
        )
        await service.poll_once(success.id)
        self.assertTrue(await service.has_current_pass(self.cycle.id, "commit-sha-1"))
        await service.poll_once(failure.id)

        self.assertFalse(await service.has_current_pass(self.cycle.id, "commit-sha-1"))

    async def test_missing_ci_check_id_is_canonicalized_for_durable_identity(self):
        provider = _CiProvider([])
        service = FactoryCiService(session_factory=self.sessions, providers={"github": provider})
        first = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-1",
            external_run_id="run-no-check",
        )
        second = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-1",
            external_run_id="run-no-check",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(first.check_id, "")

    async def test_ci_pass_is_revision_bound_and_poll_once_does_not_wait_or_loop(self):
        provider = _CiProvider(
            [
                CiObservation(status="COMPLETED", conclusion="SUCCESS"),
                CiObservation(status="COMPLETED", conclusion="SUCCESS"),
            ]
        )
        service = FactoryCiService(session_factory=self.sessions, providers={"github": provider})
        first = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-1",
            external_run_id="run-1",
        )
        await service.poll_once(first.id)

        self.assertTrue(await service.has_current_pass(self.cycle.id, "commit-sha-1"))
        self.assertFalse(await service.has_current_pass(self.cycle.id, "commit-sha-2"))
        self.assertEqual(len(provider.calls), 1)

        second = await service.begin_tracking(
            run_id=self.run.id,
            cycle_id=self.cycle.id,
            provider="github",
            repository="heidi-dang/computer",
            revision="commit-sha-2",
            external_run_id="run-2",
        )
        await service.poll_once(second.id)
        self.assertTrue(await service.has_current_pass(self.cycle.id, "commit-sha-2"))
        self.assertEqual(len(provider.calls), 2)


class GitHubActionsCliProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_is_bounded_to_exact_revision_and_keeps_latest_runs_first(self):
        revision = "a" * 40
        runner = _GhRunner(
            [
                (
                    0,
                    json.dumps(
                        [
                            {
                                "databaseId": 101,
                                "headSha": revision,
                                "workflowName": "Tests",
                                "status": "completed",
                                "conclusion": "success",
                                "url": "https://github.com/example/repo/actions/runs/101",
                            },
                            {
                                "databaseId": 102,
                                "headSha": revision,
                                "workflowName": "Tests",
                                "status": "in_progress",
                                "conclusion": "",
                                "url": "https://github.com/example/repo/actions/runs/102",
                            },
                            {
                                "databaseId": 999,
                                "headSha": "b" * 40,
                                "workflowName": "Other revision",
                            },
                        ]
                    ),
                    "",
                )
            ]
        )
        provider = GitHubActionsCliProvider(command_runner=runner)

        identities = await provider.discover(repository="example/repo", revision=revision)

        self.assertEqual([item.external_run_id for item in identities], ["102", "101"])
        self.assertTrue(all(item.workflow == "Tests" for item in identities))
        argv = runner.calls[0][0]
        self.assertEqual(argv[:3], ("gh", "run", "list"))
        self.assertIn(revision, argv)

    async def test_observation_rejects_stale_revision_and_never_returns_provider_stderr(self):
        revision = "c" * 40
        stale = _GhRunner(
            [
                (
                    0,
                    json.dumps(
                        {
                            "databaseId": 55,
                            "headSha": "d" * 40,
                            "workflowName": "Tests",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ),
                    "secret stderr",
                )
            ]
        )
        provider = GitHubActionsCliProvider(command_runner=stale)
        with self.assertRaises(FactoryCiError) as caught:
            await provider.observe(
                CiPollRequest(
                    provider="github",
                    repository="example/repo",
                    revision=revision,
                    external_run_id="55",
                    check_id="Tests",
                )
            )
        self.assertEqual(caught.exception.code, "FACTORY_CI_STALE_REVISION")
        self.assertNotIn("secret", str(caught.exception))

    async def test_observation_maps_github_terminal_failure_without_fetching_logs(self):
        revision = "e" * 40
        runner = _GhRunner(
            [
                (
                    0,
                    json.dumps(
                        {
                            "databaseId": 77,
                            "headSha": revision,
                            "workflowName": "Tests",
                            "status": "completed",
                            "conclusion": "failure",
                            "url": "https://github.com/example/repo/actions/runs/77",
                        }
                    ),
                    "",
                )
            ]
        )
        provider = GitHubActionsCliProvider(command_runner=runner)

        observation = await provider.observe(
            CiPollRequest(
                provider="github",
                repository="example/repo",
                revision=revision,
                external_run_id="77",
                check_id="Tests",
            )
        )

        self.assertEqual(observation.status, "completed")
        self.assertEqual(observation.conclusion, "failure")
        self.assertIn("Tests", observation.failure_summary or "")
        self.assertEqual(runner.calls[0][0][:3], ("gh", "run", "view"))

    async def test_cli_failure_is_generic_and_does_not_leak_response_body(self):
        runner = _GhRunner([(1, "", "gh token=super-secret")])
        provider = GitHubActionsCliProvider(command_runner=runner)

        with self.assertRaises(FactoryCiError) as caught:
            await provider.discover(repository="example/repo", revision="f" * 40)

        self.assertEqual(caught.exception.code, "FACTORY_CI_PROVIDER_FAILURE")
        self.assertNotIn("super-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
