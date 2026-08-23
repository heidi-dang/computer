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
from cptr.flowdeck.config import FlowDeckMode
from cptr.flowdeck.durable import DurableFlowDeck
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


if __name__ == "__main__":
    unittest.main()