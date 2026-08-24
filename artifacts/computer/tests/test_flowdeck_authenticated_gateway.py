import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from cptr.flowdeck.authenticated_gateway import (
    AuthenticatedGatewayError,
    SpecialistDispatchRequest,
    dispatch_authenticated_specialist,
)
from cptr.flowdeck.coding import CodingRequest, run_browser_debugger, run_coding_specialist
from cptr.flowdeck.config import FlowDeckMode
from cptr.flowdeck.durable import DurableFlowDeck
from cptr.flowdeck.execution import MapperRequest, run_read_only_specialist
from cptr.models.base import Base
from cptr.models.workspaces import Workspace
from cptr.utils.config import AuthResult


def auth_request(user_id=None):
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/__internal__",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("internal", 0),
            "scheme": "http",
            "state": {},
        }
    )
    if user_id:
        request.state.auth = AuthResult(user_id=user_id, role="user")
    return request


class AuthenticatedGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        with tempfile.NamedTemporaryFile(delete=False) as db:
            self.db_path = db.name
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.store = DurableFlowDeck(
            async_sessionmaker(self.engine, expire_on_commit=False)
        )
        async with self.store.session_factory() as session:
            session.add(
                Workspace(
                    user_id="owner",
                    path=str(self.root),
                    name="owned",
                    data={},
                    created_at=1,
                )
            )
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        os.unlink(self.db_path)
        self.temp.cleanup()

    def dispatch(self, role="debug-specialist", workspace=None):
        return SpecialistDispatchRequest(
            role=role,
            request_key=f"gateway-{role}",
            task="inspect",
            workspace=str(workspace or self.root),
            model="free",
            connection={},
            parent_chat_id="parent",
        )

    async def test_missing_wrong_and_forged_identity_fail_closed(self):
        for request, workspace in (
            (auth_request(), self.root),
            (auth_request("other"), self.root),
            (auth_request("owner"), self.root / "forged"),
        ):
            with self.assertRaises(AuthenticatedGatewayError):
                await dispatch_authenticated_specialist(
                    request, self.dispatch(workspace=workspace), store=self.store
                )

    async def test_stale_and_ambiguous_ownership_fail_closed(self):
        async with self.store.session_factory() as session:
            await session.execute(
                __import__("sqlalchemy").delete(Workspace).where(Workspace.user_id == "owner")
            )
            await session.commit()
        with self.assertRaises(AuthenticatedGatewayError):
            await dispatch_authenticated_specialist(
                auth_request("owner"), self.dispatch(), store=self.store
            )

    async def test_gateway_derives_identity_and_dispatches_read_only_role(self):
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": FlowDeckMode.READ_ONLY.value,
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch(
                "cptr.utils.tools._create_subagent_chat",
                new=AsyncMock(
                    return_value=(
                        type("Chat", (), {"id": "chat"})(),
                        None,
                        type("Message", (), {"id": "message"})(),
                    )
                ),
            ),
            patch(
                "cptr.utils.tools._run_existing_subagent_chat",
                new=AsyncMock(return_value="read-only result"),
            ),
        ):
            result = await dispatch_authenticated_specialist(
                auth_request("owner"), self.dispatch(), store=self.store
            )
        self.assertEqual(result, "read-only result")

    async def test_all_supported_roles_use_the_authenticated_gateway(self):
        roles = (
            "backend-coder",
            "frontend-coder",
            "browser-debugger",
            "debug-specialist",
            "mapper",
            "researcher",
            "architect",
            "reviewer",
            "security-auditor",
        )
        with patch.dict(
            os.environ,
            {
                "CPTR_FLOWDECK_ENABLED": "true",
                "CPTR_FLOWDECK_MODE": "controlled",
                "CPTR_FLOWDECK_GOVERNANCE": "strict",
                "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                "CPTR_FLOWDECK_CODING_ROLE": "backend-coder",
            },
            clear=False,
        ), patch(
            "cptr.flowdeck.authenticated_gateway._native_run_coding_specialist",
            new=AsyncMock(return_value="coder"),
        ), patch(
            "cptr.flowdeck.authenticated_gateway._native_run_browser_debugger",
            new=AsyncMock(return_value="browser"),
        ), patch(
            "cptr.flowdeck.authenticated_gateway._native_run_read_only_specialist",
            new=AsyncMock(return_value="readonly"),
        ):
            for role in roles:
                result = await dispatch_authenticated_specialist(
                    auth_request("owner"), self.dispatch(role=role), store=self.store
                )
                self.assertIn(result, {"coder", "browser", "readonly"})

    async def test_coding_dispatch_can_use_only_an_authenticated_repository_worktree(self):
        import subprocess
        from dataclasses import replace

        subprocess.check_call(("git", "-C", str(self.root), "init", "-q"))
        subprocess.check_call(("git", "-C", str(self.root), "config", "user.name", "test"))
        subprocess.check_call(("git", "-C", str(self.root), "config", "user.email", "test@example.invalid"))
        (self.root / "README.md").write_text("base\n")
        subprocess.check_call(("git", "-C", str(self.root), "add", "README.md"))
        subprocess.check_call(("git", "-C", str(self.root), "commit", "-qm", "base"))
        from cptr.flowdeck.worktrees import create_worktree, remove_worktree

        handle = await create_worktree(
            canonical_workspace=str(self.root),
            run_id="gateway",
            node_key="backend",
        )
        try:
            with patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                    "CPTR_FLOWDECK_MUTATING_AGENTS": "true",
                    "CPTR_FLOWDECK_CODING_ROLE": "backend-coder",
                },
                clear=False,
            ), patch(
                "cptr.flowdeck.authenticated_gateway._native_run_coding_specialist",
                new=AsyncMock(return_value="coder"),
            ) as native:
                result = await dispatch_authenticated_specialist(
                    auth_request("owner"),
                    replace(
                        self.dispatch(role="backend-coder"),
                        execution_workspace=handle.path,
                    ),
                    store=self.store,
                )
            self.assertEqual(result, "coder")
            request = native.call_args.args[0]
            self.assertEqual(request.workspace, handle.path)
            self.assertEqual(request.canonical_workspace, str(self.root))
        finally:
            await remove_worktree(handle)

        with self.assertRaises(AuthenticatedGatewayError):
            await dispatch_authenticated_specialist(
                auth_request("owner"),
                replace(
                    self.dispatch(role="backend-coder"),
                    execution_workspace=str(Path(self.temp.name).parent),
                ),
                store=self.store,
            )

    async def test_compatibility_boundaries_reject_missing_authenticated_context(self):
        coding = CodingRequest(
            role="backend-coder",
            workspace=str(self.root),
            user_id="forged",
            task="x",
            request_key="direct-coding",
        )
        mapper = MapperRequest(
            request_key="direct-read",
            task="x",
            workspace=str(self.root),
            user_id="forged",
            model="free",
            connection={},
            parent_chat_id="p",
        )
        with self.assertRaises(TypeError):
            await run_coding_specialist(
                coding, model="free", connection={}, parent_chat_id="p", store=self.store
            )
        with self.assertRaises(TypeError):
            await run_browser_debugger(
                coding, model="free", connection={}, parent_chat_id="p", store=self.store
            )
        with self.assertRaises(TypeError):
            await run_read_only_specialist(mapper, "mapper", store=self.store)


if __name__ == "__main__":
    unittest.main()