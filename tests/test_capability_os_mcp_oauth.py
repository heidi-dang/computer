from __future__ import annotations

import unittest
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from cptr.models import Base
from cptr.routers.capability_os import mcp_oauth_callback_router
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    create_artifact,
)
from cptr.services.capability_os.mcp_oauth import (
    ConfigRemoteMcpOAuthProfileProvider,
    McpOAuthCredentialProvider,
    McpOAuthError,
    McpOAuthService,
    McpOAuthStore,
)
from cptr.services.capability_os.mcp_remote import PublicHttpsEndpointValidator, RemoteMcpError
from cptr.services.capability_os.store import SqlCapabilityOsStore


class _AllowHttpsValidator:
    async def validate(self, raw_url: str) -> str:
        value = str(raw_url).strip()
        if not value.startswith("https://"):
            raise McpOAuthError("HTTPS required")
        return value


def _encrypt(value: str, _secret: str) -> str:
    return "encrypted:" + value[::-1]


def _decrypt(value: str, _secret: str) -> str:
    if not value.startswith("encrypted:"):
        raise ValueError("not encrypted")
    return value[len("encrypted:") :][::-1]


class CapabilityOsMcpOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.artifacts = SqlCapabilityOsStore(session_factory=self.sessions)
        self.oauth_store = McpOAuthStore(
            session_factory=self.sessions,
            secret_getter=lambda: "test-secret",
            encryptor=_encrypt,
            decryptor=_decrypt,
        )
        self.now = [1_000_000]
        self.profile_config = [
            {
                "profileId": "logs-oauth",
                "serverId": "io.example/logs",
                "remoteUrl": "https://mcp.example/mcp",
                "redirectUri": "https://cptr.example/api/oauth/mcp/callback",
                "clientName": "CPTR Test",
            }
        ]
        profiles = ConfigRemoteMcpOAuthProfileProvider(
            config_getter=lambda _key: self._return(self.profile_config)
        )
        self.requests: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append((request.method, str(request.url)))
            url = str(request.url)
            if request.method == "POST" and url == "https://mcp.example/mcp":
                return httpx.Response(
                    401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="https://auth.example/resource-metadata", '
                            'scope="logs.read"'
                        )
                    },
                    request=request,
                )
            if request.method == "GET" and url == "https://auth.example/resource-metadata":
                return httpx.Response(
                    200,
                    json={
                        "resource": "https://mcp.example/mcp",
                        "authorization_servers": ["https://auth.example"],
                        "scopes_supported": ["logs.read"],
                    },
                    request=request,
                )
            if request.method == "GET" and url in {
                "https://auth.example/.well-known/oauth-authorization-server",
                "https://auth.example/.well-known/openid-configuration",
            }:
                return httpx.Response(
                    200,
                    json={
                        "issuer": "https://auth.example",
                        "authorization_endpoint": "https://auth.example/authorize",
                        "token_endpoint": "https://auth.example/token",
                        "registration_endpoint": "https://auth.example/register",
                        "scopes_supported": ["logs.read"],
                        "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "token_endpoint_auth_methods_supported": ["client_secret_post"],
                    },
                    request=request,
                )
            if request.method == "POST" and url == "https://auth.example/register":
                return httpx.Response(
                    201,
                    json={
                        "client_id": "client-123",
                        "client_secret": "client-secret",
                        "token_endpoint_auth_method": "client_secret_post",
                        "redirect_uris": ["https://cptr.example/api/oauth/mcp/callback"],
                        "grant_types": ["authorization_code", "refresh_token"],
                        "response_types": ["code"],
                        "client_name": "CPTR Test",
                    },
                    request=request,
                )
            if request.method == "POST" and url == "https://auth.example/token":
                body = request.content.decode("utf-8")
                if "grant_type=refresh_token" in body:
                    return httpx.Response(
                        200,
                        json={
                            "access_token": "access-rotated",
                            "refresh_token": "refresh-rotated",
                            "token_type": "Bearer",
                            "expires_in": 120,
                            "scope": "logs.read",
                        },
                        request=request,
                    )
                return httpx.Response(
                    200,
                    json={
                        "access_token": "access-initial",
                        "refresh_token": "refresh-initial",
                        "token_type": "Bearer",
                        "expires_in": 60,
                        "scope": "logs.read",
                    },
                    request=request,
                )
            return httpx.Response(404, request=request)

        transport = httpx.MockTransport(handler)
        self.service = McpOAuthService(
            artifacts=self.artifacts,
            oauth_store=self.oauth_store,
            profiles=profiles,
            endpoint_validator=_AllowHttpsValidator(),
            clock_ms=lambda: self.now[0],
            timeout_seconds=2,
        )

        async def client():
            return httpx.AsyncClient(
                transport=transport,
                timeout=2,
                follow_redirects=False,
                trust_env=False,
            )

        self.service._http_client = client
        permission = CapabilityRequest("mcp.invoke", "mcp:io.example/logs/*")
        adapter = create_artifact(
            artifact_id="mcp.adapter.logs",
            version="1",
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec={
                "serverId": "io.example/logs",
                "remote": {"url": "https://mcp.example/mcp", "transport": "streamable-http"},
                "authentication": {
                    "mechanism": "oauth2",
                    "logicalName": "logs-oauth",
                    "source": "operator-profile",
                },
                "permissions": [permission.to_dict()],
            },
            created_at="2026-09-08T10:00:00Z",
            user_id="user-1",
            task_origin="task-1",
            state=ArtifactState.EPHEMERAL,
        )
        await self.artifacts.persist_artifact(adapter)
        self.adapter_digest = adapter.metadata.content_digest

    @staticmethod
    async def _return(value):
        return value

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _start(self):
        result = await self.service.start(
            user_id="user-1",
            task_id="task-1",
            artifact_digest=self.adapter_digest,
        )
        self.assertEqual(result.status, "pending")
        self.assertIsNotNone(result.flow_id)
        self.assertIsNotNone(result.authorization_url)
        return result

    async def test_pkce_flow_persists_only_encrypted_secrets_and_rejects_replay(self):
        started = await self._start()
        parsed = urlsplit(started.authorization_url or "")
        params = parse_qs(parsed.query)
        self.assertEqual(parsed.geturl().split("?", 1)[0], "https://auth.example/authorize")
        self.assertEqual(params["client_id"], ["client-123"])
        self.assertEqual(params["redirect_uri"], ["https://cptr.example/api/oauth/mcp/callback"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["resource"], ["https://mcp.example/mcp"])
        self.assertEqual(params["scope"], ["logs.read"])
        state = params["state"][0]
        flow = await self.oauth_store.get_flow(started.flow_id or "")
        self.assertIsNotNone(flow)
        self.assertNotEqual(flow.state_hash, state)
        self.assertTrue(flow.state_hash.startswith("sha256:"))
        self.assertTrue(flow.code_verifier_encrypted.startswith("encrypted:"))
        self.assertNotIn("client-secret", flow.client_info_encrypted)

        completed = await self.service.complete_callback(
            state=state,
            code="authorization-code",
            issuer="https://auth.example",
        )
        self.assertEqual(completed["status"], "complete")
        derived = await self.artifacts.get_artifact(
            completed["artifactDigest"], user_id="user-1", include_global=False
        )
        self.assertEqual(derived.spec["authentication"]["source"], "oauth2")
        logical_name = derived.spec["authentication"]["logicalName"]
        credential = await self.oauth_store.get_credential(logical_name)
        self.assertIsNotNone(credential)
        self.assertTrue(credential.access_token_encrypted.startswith("encrypted:"))
        self.assertNotIn("access-initial", credential.access_token_encrypted)
        self.assertNotIn("refresh-initial", credential.refresh_token_encrypted)
        self.assertEqual(
            await self.service.access_token(
                logical_name=logical_name,
                consumer="mcp.remote:https://mcp.example/mcp",
            ),
            "access-initial",
        )
        with self.assertRaisesRegex(McpOAuthError, "no longer pending"):
            await self.service.complete_callback(state=state, code="second-code")

    async def test_callback_claim_prevents_exchange_when_flow_is_already_in_progress(self):
        started = await self._start()
        state = parse_qs(urlsplit(started.authorization_url or "").query)["state"][0]
        changed = await self.oauth_store.set_flow_status(
            started.flow_id or "",
            expected_status="pending",
            status="exchanging",
            now_ms=self.now[0],
        )
        self.assertTrue(changed)
        before = [item for item in self.requests if item == ("POST", "https://auth.example/token")]
        with self.assertRaisesRegex(McpOAuthError, "no longer pending"):
            await self.service.complete_callback(state=state, code="authorization-code")
        after = [item for item in self.requests if item == ("POST", "https://auth.example/token")]
        self.assertEqual(before, after)

    async def test_issuer_mismatch_fails_closed_without_creating_credential(self):
        started = await self._start()
        state = parse_qs(urlsplit(started.authorization_url or "").query)["state"][0]
        with self.assertRaisesRegex(McpOAuthError, "issuer mismatch"):
            await self.service.complete_callback(
                state=state,
                code="authorization-code",
                issuer="https://attacker.example",
            )
        flow = await self.oauth_store.get_flow(started.flow_id or "")
        self.assertEqual(flow.status, "failed")
        self.assertEqual(flow.error_code, "issuer-mismatch")
        derived = await self.artifacts.get_artifact(started.artifact_digest)
        logical_name = derived.spec["authentication"]["logicalName"]
        self.assertIsNone(await self.oauth_store.get_credential(logical_name))

    async def test_refresh_rotates_tokens_and_local_revoke_removes_broker_authority(self):
        started = await self._start()
        state = parse_qs(urlsplit(started.authorization_url or "").query)["state"][0]
        completed = await self.service.complete_callback(state=state, code="authorization-code")
        derived = await self.artifacts.get_artifact(completed["artifactDigest"])
        logical_name = derived.spec["authentication"]["logicalName"]
        provider = McpOAuthCredentialProvider(self.service)
        consumer = "mcp.remote:https://mcp.example/mcp"
        self.assertTrue(await provider.allows(logical_name=logical_name, consumer=consumer))
        self.now[0] += 40_000
        self.assertEqual(
            await provider.fetch(
                logical_name=logical_name,
                task_id="task-1",
                lease_id="lease-1",
                consumer=consumer,
            ),
            "access-rotated",
        )
        credential = await self.oauth_store.get_credential(logical_name)
        self.assertNotIn("access-rotated", credential.access_token_encrypted)
        self.assertNotIn("refresh-rotated", credential.refresh_token_encrypted)
        self.assertFalse(await self.service.revoke(user_id="other-user", logical_name=logical_name))
        self.assertTrue(await self.service.revoke(user_id="user-1", logical_name=logical_name))
        self.assertFalse(await provider.allows(logical_name=logical_name, consumer=consumer))
        self.assertIsNone(
            await provider.fetch(
                logical_name=logical_name,
                task_id="task-1",
                lease_id="lease-1",
                consumer=consumer,
            )
        )

    async def test_status_is_user_and_task_scoped_and_expired_exchange_is_reconciled(self):
        started = await self._start()
        with self.assertRaisesRegex(McpOAuthError, "not found"):
            await self.service.status(
                user_id="other-user",
                task_id="task-1",
                flow_id=started.flow_id or "",
            )
        await self.oauth_store.set_flow_status(
            started.flow_id or "",
            expected_status="pending",
            status="exchanging",
            now_ms=self.now[0],
        )
        self.now[0] += 10 * 60 * 1000 + 1
        status = await self.service.status(
            user_id="user-1",
            task_id="task-1",
            flow_id=started.flow_id or "",
        )
        self.assertEqual(status["status"], "expired")
        self.assertEqual(status["errorCode"], "flow-expired")

    async def test_private_network_oauth_endpoints_are_rejected_before_http(self):
        validator = PublicHttpsEndpointValidator()
        with self.assertRaisesRegex(RemoteMcpError, "public address"):
            await validator.validate("https://127.0.0.1/mcp")
        with self.assertRaisesRegex(RemoteMcpError, "localhost"):
            await validator.validate("https://localhost/mcp")

    async def test_dynamic_client_registration_failure_creates_no_usable_credential(self):
        async def fail_client_info(**_kwargs):
            raise McpOAuthError("MCP OAuth dynamic client registration failed")

        self.service._client_info = fail_client_info
        with self.assertRaisesRegex(McpOAuthError, "registration failed"):
            await self.service.start(
                user_id="user-1",
                task_id="task-1",
                artifact_digest=self.adapter_digest,
            )
        artifacts = await self.artifacts.list_artifacts(
            user_id="user-1",
            include_global=False,
            kinds=(ArtifactKind.MCP_ADAPTER.value,),
        )
        derived = next(
            row
            for row in artifacts
            if isinstance(row.spec.get("authentication"), dict)
            and row.spec["authentication"].get("source") == "oauth2"
        )
        self.assertEqual(derived.state, ArtifactState.EPHEMERAL.value)
        logical_name = derived.spec["authentication"]["logicalName"]
        self.assertIsNone(await self.oauth_store.get_credential(logical_name))
        self.assertFalse(
            any(url == "https://auth.example/token" for _method, url in self.requests)
        )

    async def test_callback_router_returns_only_generic_non_cacheable_result(self):
        class FakeOAuth:
            async def complete_callback(self, **kwargs):
                self.kwargs = kwargs
                return {"access_token": "must-never-render"}

        fake = FakeOAuth()
        app = FastAPI()
        app.state.capability_os_control_service = SimpleNamespace(mcp_oauth=fake)
        app.include_router(mcp_oauth_callback_router)
        with TestClient(app) as client:
            response = client.get(
                "/api/oauth/mcp/callback",
                params={"state": "s" * 32, "code": "secret-code"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret-code", response.text)
        self.assertNotIn("must-never-render", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(fake.kwargs["state"], "s" * 32)


if __name__ == "__main__":
    unittest.main()
