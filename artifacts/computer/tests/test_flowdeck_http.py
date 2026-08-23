import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.app import app
from cptr.flowdeck.coordinator import CoordinatorResult
from cptr.models import Auth, Base, Config, User, Workspace
from cptr.utils import db as db_module


class FlowDeckProductionHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root_a = Path(self.temp.name, "workspace-a").resolve()
        self.root_b = Path(self.temp.name, "workspace-b").resolve()
        self.root_a.mkdir()
        self.root_b.mkdir()
        self.db_file = Path(self.temp.name, "http.db")
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_file}")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.previous_engine = db_module._engine
        self.previous_session_factory = db_module._async_session
        db_module._engine = self.engine
        db_module._async_session = self.session_factory

        self.tokens = {"user-a": "token-a", "user-b": "token-b"}
        async with self.session_factory() as session:
            session.add_all(
                [
                    User(id="user-a", role="user", created_at=1),
                    User(id="user-b", role="user", created_at=1),
                    Auth(user_id="user-a", username="a", password=None),
                    Auth(user_id="user-b", username="b", password=None),
                    Workspace(
                        user_id="user-a",
                        path=str(self.root_a),
                        name="workspace-a",
                        data={},
                        created_at=1,
                    ),
                    Workspace(
                        user_id="user-b",
                        path=str(self.root_b),
                        name="workspace-b",
                        data={},
                        created_at=1,
                    ),
                    Config(
                        key="api_keys",
                        value=[
                            {
                                "key_hash": hashlib.sha256(token.encode()).hexdigest(),
                                "user_id": user_id,
                            }
                            for user_id, token in self.tokens.items()
                        ],
                    ),
                ]
            )
            await session.commit()

        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        db_module._engine = self.previous_engine
        db_module._async_session = self.previous_session_factory
        await self.engine.dispose()
        self.temp.cleanup()

    def headers(self, user_id="user-a"):
        return {"Authorization": f"Bearer {self.tokens[user_id]}"}

    async def request_run(self, user_id="user-a", key="retry-key-123", workspace=None):
        return await self.client.post(
            "/v1/flowdeck/orchestrations",
            headers={**self.headers(user_id), "Idempotency-Key": key},
            json={
                "workspace": str(workspace or self.root_a),
                "objective": "review the repository",
            },
        )

    async def test_bearer_auth_covers_submit_status_and_cancel(self):
        async def submit(request, *, authenticated_request, store):
            run, _ = await store.create_run(
                request_key=request.request_key,
                owner=authenticated_request.state.auth.user_id,
                workspace=request.workspace,
            )
            return CoordinatorResult("pending", run.id, (), ())

        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_COORDINATOR_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch(
                "cptr.routers.flowdeck._resolve_model",
                new=AsyncMock(
                    return_value=(
                        SimpleNamespace(
                            kind="api", runtime_model="test-model", connection={}
                        ),
                        "test-model",
                    )
                ),
            ),
            patch("cptr.routers.flowdeck.run_heidi_coordinator", new=submit),
        ):
            created = await self.request_run()
            self.assertEqual(created.status_code, 200)
            run_id = created.json()["run_id"]

            status_response = await self.client.get(
                f"/v1/flowdeck/orchestrations/{run_id}",
                params={"workspace": str(self.root_a)},
                headers=self.headers(),
            )
            self.assertEqual(status_response.status_code, 200)
            self.assertEqual(status_response.json()["status"], "pending")

            for headers in ({}, {"Authorization": "Bearer invalid-token"}):
                unauthorized_status = await self.client.get(
                    f"/v1/flowdeck/orchestrations/{run_id}",
                    params={"workspace": str(self.root_a)},
                    headers=headers,
                )
                self.assertEqual(unauthorized_status.status_code, 401)
                unauthorized_cancel = await self.client.post(
                    f"/v1/flowdeck/orchestrations/{run_id}/cancel",
                    params={"workspace": str(self.root_a)},
                    headers=headers,
                )
                self.assertEqual(unauthorized_cancel.status_code, 401)

            for method in ("get", "post"):
                cross_user = await getattr(self.client, method)(
                    f"/v1/flowdeck/orchestrations/{run_id}"
                    + ("/cancel" if method == "post" else ""),
                    params={"workspace": str(self.root_a)},
                    headers=self.headers("user-b"),
                )
                self.assertEqual(cross_user.status_code, 403)

            cancelled = await self.client.post(
                f"/v1/flowdeck/orchestrations/{run_id}/cancel",
                params={"workspace": str(self.root_a)},
                headers=self.headers(),
            )
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json()["status"], "cancelled")

    async def test_missing_invalid_and_cross_user_access_fail_closed(self):
        response = await self.client.post(
            "/v1/flowdeck/orchestrations",
            headers={"Idempotency-Key": "missing-auth-123"},
            json={"workspace": str(self.root_a), "objective": "review the repository"},
        )
        self.assertEqual(response.status_code, 401)

        response = await self.client.get(
            "/v1/flowdeck/orchestrations/not-a-run",
            params={"workspace": str(self.root_a)},
            headers={"Authorization": "Bearer invalid-token"},
        )
        self.assertEqual(response.status_code, 401)

        with patch.dict(
            os.environ,
            {
                "CPTR_FLOWDECK_ENABLED": "true",
                "CPTR_FLOWDECK_COORDINATOR_ENABLED": "true",
                "CPTR_FLOWDECK_MODE": "controlled",
                "CPTR_FLOWDECK_GOVERNANCE": "strict",
            },
            clear=False,
        ):
            response = await self.client.post(
                "/v1/flowdeck/orchestrations",
                headers={**self.headers("user-b"), "Idempotency-Key": "cross-user-123"},
                json={"workspace": str(self.root_a), "objective": "review the repository"},
            )
        self.assertEqual(response.status_code, 403)

    async def test_retry_and_reconnect_key_reuse_persists_one_run(self):
        calls = []
        created_flags = []

        async def submit(request, *, authenticated_request, store):
            calls.append(request.request_key)
            run, created = await store.create_run(
                request_key=request.request_key,
                owner=authenticated_request.state.auth.user_id,
                workspace=request.workspace,
            )
            created_flags.append(created)
            return CoordinatorResult("pending", run.id, (), ())

        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "true",
                    "CPTR_FLOWDECK_COORDINATOR_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch(
                "cptr.routers.flowdeck._resolve_model",
                new=AsyncMock(
                    return_value=(
                        SimpleNamespace(
                            kind="api", runtime_model="test-model", connection={}
                        ),
                        "test-model",
                    )
                ),
            ),
            patch("cptr.routers.flowdeck.run_heidi_coordinator", new=submit),
        ):
            first = await self.request_run(key="same-key-123")
            retry = await self.request_run(key="same-key-123")
            reconnect = await self.request_run(key="same-key-123")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(reconnect.status_code, 200)
        self.assertEqual(
            {first.json()["run_id"], retry.json()["run_id"], reconnect.json()["run_id"]},
            {first.json()["run_id"]},
        )
        self.assertEqual(calls, ["same-key-123"] * 3)
        self.assertEqual(created_flags, [True, False, False])
        async with self.session_factory() as session:
            from cptr.models.flowdeck import FlowDeckRun

            runs = (await session.execute(select(FlowDeckRun))).scalars().all()
            self.assertEqual(len(runs), 1)

    async def test_disabled_route_has_no_durable_side_effect(self):
        with (
            patch.dict(
                os.environ,
                {
                    "CPTR_FLOWDECK_ENABLED": "false",
                    "CPTR_FLOWDECK_COORDINATOR_ENABLED": "true",
                    "CPTR_FLOWDECK_MODE": "controlled",
                    "CPTR_FLOWDECK_GOVERNANCE": "strict",
                },
                clear=False,
            ),
            patch("cptr.routers.flowdeck.run_heidi_coordinator", new=AsyncMock()) as coordinator,
        ):
            response = await self.request_run(key="disabled-key-123")

        self.assertEqual(response.status_code, 404)
        coordinator.assert_not_awaited()
        async with self.session_factory() as session:
            from cptr.models.flowdeck import FlowDeckRun

            self.assertEqual(
                (await session.execute(select(FlowDeckRun))).scalars().all(),
                [],
            )


if __name__ == "__main__":
    unittest.main()