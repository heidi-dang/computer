import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.flowdeck.config import FlowDeckConfig
from cptr.flowdeck.contracts import FlowDeckMode
from cptr.flowdeck.durable import DurableFlowDeck, RunStatus
from cptr.flowdeck.tester import (
    TEST_CHECKS,
    TesterPolicyError,
    TesterRequest,
    run_tester,
    validate_tester_request,
)
from cptr.models.base import Base


class TesterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        db_fd, self.db_path = tempfile.mkstemp()
        os.close(db_fd)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False),
            clock=lambda: 1000,
        )
        self.request = TesterRequest(
            request_key="tester-request",
            workspace=self.workspace.name,
            user_id="user-1",
            check="tests",
            trusted_repository=True,
            repository_identity="local-trusted-repo:v1",
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_path)
        self.workspace.cleanup()

    def test_tester_requires_trust_and_controlled_mode(self):
        config = FlowDeckConfig(enabled=True, mode=FlowDeckMode.CONTROLLED, governance="strict")
        self.assertEqual(
            validate_tester_request(self.request, config),
            Path(self.workspace.name).resolve(),
        )
        for request in (
            self.request.__class__(**{**self.request.__dict__, "trusted_repository": False}),
            self.request.__class__(**{**self.request.__dict__, "repository_identity": ""}),
            self.request.__class__(**{**self.request.__dict__, "check": "shell"}),
        ):
            with self.assertRaises(TesterPolicyError):
                validate_tester_request(request, config)
        with self.assertRaises(TesterPolicyError):
            validate_tester_request(
                self.request,
                FlowDeckConfig(enabled=True, mode=FlowDeckMode.READ_ONLY, governance="strict"),
            )

    async def test_tester_records_authoritative_exit_evidence(self):
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch(
                "cptr.flowdeck.tester._run_check",
                new=AsyncMock(return_value=(0, b"ok", b"")),
            ),
        ):
            result = await run_tester(self.request, store=self.store)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["evidence"]["exit_code"], 0)
        self.assertEqual(result["evidence"]["observation"], "verifier_check")
        run, created = await self.store.create_run(
            request_key=self.request.request_key,
            owner=self.request.user_id,
            workspace=self.request.workspace,
        )
        self.assertFalse(created)
        self.assertEqual(run.status, RunStatus.SUCCEEDED.value)

    async def test_tester_nonzero_exit_is_authoritative_failure(self):
        request = self.request.__class__(
            **{**self.request.__dict__, "request_key": "tester-failed"}
        )
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch(
                "cptr.flowdeck.tester._run_check",
                new=AsyncMock(return_value=(1, b"failed", b"error")),
            ),
        ):
            result = await run_tester(request, store=self.store)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["evidence"]["exit_code"], 1)

    async def test_tester_cancellation_orphans_durable_run(self):
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch("cptr.flowdeck.tester._run_check", new=cancelled),
            self.assertRaises(asyncio.CancelledError),
        ):
            await run_tester(self.request, store=self.store)
        run, created = await self.store.create_run(
            request_key=self.request.request_key,
            owner=self.request.user_id,
            workspace=self.request.workspace,
        )
        self.assertFalse(created)
        self.assertEqual(run.status, RunStatus.ORPHANED.value)

    def test_structured_checks_have_no_shell_string(self):
        self.assertEqual(TEST_CHECKS, {"tests", "build", "typecheck", "lint"})
