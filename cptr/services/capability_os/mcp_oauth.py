"""Server-owned OAuth 2.1 lifecycle for authenticated remote MCP adapters.

OAuth never widens the MCP surface or returns credential material.  Operator
configuration binds an exact MCP server identity to an OAuth profile; CPTR then
owns discovery, DCR/CIMD, PKCE/state, encrypted persistence, refresh, and the
logical credential consumed by :class:`CredentialBroker`.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import quote, urlencode, urljoin, urlsplit

import httpx
from pydantic import AnyUrl
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from cptr.models import CapabilityOsMcpOAuthCredential, CapabilityOsMcpOAuthFlow
from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    create_artifact,
)
from cptr.services.capability_os.mcp_remote import PublicHttpsEndpointValidator
from cptr.services.capability_os.store import SqlCapabilityOsStore
from cptr.utils.config import _get_jwt_secret
from cptr.utils.crypto import decrypt_key, encrypt_key
from cptr.utils.db import get_session_factory


class McpOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteMcpOAuthProfile:
    profile_id: str
    server_id: str
    remote_url: str
    redirect_uri: str
    client_name: str
    client_metadata_url: str | None = None

    def __post_init__(self) -> None:
        values = {
            "profile_id": str(self.profile_id).strip(),
            "server_id": str(self.server_id).strip(),
            "remote_url": str(self.remote_url).strip(),
            "redirect_uri": str(self.redirect_uri).strip(),
            "client_name": str(self.client_name).strip(),
        }
        if not all(values.values()):
            raise ValueError("MCP OAuth profile fields must not be blank")
        if any(len(value) > 4096 for value in values.values()):
            raise ValueError("MCP OAuth profile field exceeds size limit")
        metadata_url = str(self.client_metadata_url or "").strip() or None
        object.__setattr__(self, "profile_id", values["profile_id"])
        object.__setattr__(self, "server_id", values["server_id"])
        object.__setattr__(self, "remote_url", values["remote_url"])
        object.__setattr__(self, "redirect_uri", values["redirect_uri"])
        object.__setattr__(self, "client_name", values["client_name"])
        object.__setattr__(self, "client_metadata_url", metadata_url)


class ConfigRemoteMcpOAuthProfileProvider:
    """Resolve exact OAuth profiles from operator-owned instance Config."""

    def __init__(
        self,
        *,
        config_getter: Callable[[str], Awaitable[Any]] | None = None,
        config_key: str = "capability_os.mcp_oauth_profiles",
    ) -> None:
        self._config_getter = config_getter
        self._config_key = str(config_key).strip()
        if not self._config_key:
            raise ValueError("MCP OAuth profile config key must not be blank")

    async def _get(self) -> Any:
        if self._config_getter is not None:
            return await self._config_getter(self._config_key)
        from cptr.models import Config

        return await Config.get(self._config_key)

    async def _profiles(self) -> tuple[RemoteMcpOAuthProfile, ...]:
        raw = await self._get()
        if raw is None:
            return ()
        if not isinstance(raw, list) or len(raw) > 128:
            raise McpOAuthError("MCP OAuth profile configuration is invalid")
        allowed = {
            "enabled",
            "profileId",
            "serverId",
            "remoteUrl",
            "redirectUri",
            "clientName",
            "clientMetadataUrl",
        }
        profiles: list[RemoteMcpOAuthProfile] = []
        try:
            for item in raw:
                if not isinstance(item, dict) or item.get("enabled", True) is not True:
                    continue
                if set(item) - allowed:
                    raise ValueError("MCP OAuth profile contains unknown fields")
                profiles.append(
                    RemoteMcpOAuthProfile(
                        profile_id=str(item.get("profileId") or ""),
                        server_id=str(item.get("serverId") or ""),
                        remote_url=str(item.get("remoteUrl") or ""),
                        redirect_uri=str(item.get("redirectUri") or ""),
                        client_name=str(item.get("clientName") or "CPTR Capability OS"),
                        client_metadata_url=(
                            str(item.get("clientMetadataUrl"))
                            if item.get("clientMetadataUrl") is not None
                            else None
                        ),
                    )
                )
        except (TypeError, ValueError) as exc:
            raise McpOAuthError("MCP OAuth profile configuration is invalid") from exc
        identities = [
            (profile.profile_id, profile.server_id, profile.remote_url)
            for profile in profiles
        ]
        if len(identities) != len(set(identities)):
            raise McpOAuthError("MCP OAuth profile configuration is ambiguous")
        return tuple(profiles)

    async def resolve(
        self,
        *,
        profile_id: str,
        server_id: str,
        remote_url: str,
    ) -> RemoteMcpOAuthProfile | None:
        identity = (
            str(profile_id).strip(),
            str(server_id).strip(),
            str(remote_url).strip(),
        )
        matches = [
            profile
            for profile in await self._profiles()
            if (profile.profile_id, profile.server_id, profile.remote_url) == identity
        ]
        return matches[0] if matches else None

    async def find(
        self,
        *,
        server_id: str,
        remote_url: str,
    ) -> RemoteMcpOAuthProfile | None:
        identity = (str(server_id).strip(), str(remote_url).strip())
        matches = [
            profile
            for profile in await self._profiles()
            if (profile.server_id, profile.remote_url) == identity
        ]
        if len(matches) > 1:
            raise McpOAuthError("multiple OAuth profiles match the same remote MCP")
        return matches[0] if matches else None


@dataclass(frozen=True)
class McpOAuthStartResult:
    flow_id: str | None
    artifact_digest: str
    authorization_url: str | None
    status: str
    expires_at_ms: int | None

    def to_api(self) -> dict[str, Any]:
        return {
            "flowId": self.flow_id,
            "artifactDigest": self.artifact_digest,
            "authorizationUrl": self.authorization_url,
            "status": self.status,
            "expiresAtMs": self.expires_at_ms,
        }


class McpOAuthStore:
    """Encrypted SQL persistence for OAuth state and tokens."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker | None = None,
        secret_getter: Callable[[], str] = _get_jwt_secret,
        encryptor: Callable[[str, str], str] = encrypt_key,
        decryptor: Callable[[str, str], str] = decrypt_key,
    ) -> None:
        self._session_factory = session_factory or get_session_factory()
        self._secret_getter = secret_getter
        self._encryptor = encryptor
        self._decryptor = decryptor

    def _encrypt(self, value: str) -> str:
        text = str(value)
        if not text:
            raise McpOAuthError("OAuth secret value must not be blank")
        return self._encryptor(text, self._secret_getter())

    def _decrypt(self, value: str) -> str:
        try:
            result = self._decryptor(str(value), self._secret_getter())
        except Exception as exc:
            raise McpOAuthError("encrypted MCP OAuth state could not be decrypted") from exc
        if not result:
            raise McpOAuthError("encrypted MCP OAuth state is empty")
        return result

    def encrypt_json(self, value: dict[str, Any]) -> str:
        return self._encrypt(
            json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )

    def decrypt_json(self, value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(self._decrypt(value))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise McpOAuthError("encrypted MCP OAuth document is invalid") from exc
        if not isinstance(parsed, dict):
            raise McpOAuthError("encrypted MCP OAuth document must be an object")
        return parsed

    async def create_flow(
        self,
        *,
        user_id: str,
        task_id: str,
        artifact_digest: str,
        derived_artifact_digest: str,
        server_id: str,
        remote_url: str,
        profile_id: str,
        logical_name: str,
        redirect_uri: str,
        state: str,
        code_verifier: str,
        client_info: dict[str, Any],
        protected_resource_metadata: dict[str, Any] | None,
        oauth_metadata: dict[str, Any] | None,
        scope: str | None,
        now_ms: int,
        expires_at_ms: int,
    ) -> CapabilityOsMcpOAuthFlow:
        row = CapabilityOsMcpOAuthFlow(
            user_id=user_id,
            task_id=task_id,
            artifact_digest=artifact_digest,
            derived_artifact_digest=derived_artifact_digest,
            server_id=server_id,
            remote_url=remote_url,
            profile_id=profile_id,
            logical_name=logical_name,
            redirect_uri=redirect_uri,
            state_hash=_state_hash(state),
            code_verifier_encrypted=self._encrypt(code_verifier),
            client_info_encrypted=self.encrypt_json(client_info),
            protected_resource_metadata=protected_resource_metadata,
            oauth_metadata=oauth_metadata,
            scope=scope,
            status="pending",
            created_at_ms=int(now_ms),
            expires_at_ms=int(expires_at_ms),
        )
        async with self._session_factory() as db:
            db.add(row)
            await db.commit()
            await db.refresh(row)
            return row

    async def get_flow(self, flow_id: str) -> CapabilityOsMcpOAuthFlow | None:
        async with self._session_factory() as db:
            return await db.get(CapabilityOsMcpOAuthFlow, str(flow_id))

    async def get_flow_by_state(self, state: str) -> CapabilityOsMcpOAuthFlow | None:
        digest = _state_hash(state)
        async with self._session_factory() as db:
            return await db.scalar(
                select(CapabilityOsMcpOAuthFlow).where(
                    CapabilityOsMcpOAuthFlow.state_hash == digest
                )
            )

    async def set_flow_status(
        self,
        flow_id: str,
        *,
        expected_status: str | None,
        status: str,
        now_ms: int,
        error_code: str | None = None,
    ) -> bool:
        async with self._session_factory() as db:
            query = update(CapabilityOsMcpOAuthFlow).where(
                CapabilityOsMcpOAuthFlow.flow_id == str(flow_id)
            )
            if expected_status is not None:
                query = query.where(CapabilityOsMcpOAuthFlow.status == expected_status)
            result = await db.execute(
                query.values(
                    status=status,
                    completed_at_ms=(int(now_ms) if status in {"complete", "failed", "expired"} else None),
                    error_code=(str(error_code)[:160] if error_code else None),
                )
            )
            await db.commit()
            return bool(result.rowcount)

    async def get_credential(self, logical_name: str) -> CapabilityOsMcpOAuthCredential | None:
        async with self._session_factory() as db:
            return await db.get(CapabilityOsMcpOAuthCredential, str(logical_name))

    async def upsert_credential(
        self,
        *,
        logical_name: str,
        user_id: str,
        profile_id: str,
        server_id: str,
        remote_url: str,
        consumer: str,
        access_token: str,
        refresh_token: str | None,
        token_type: str,
        scope: str | None,
        expires_at_ms: int | None,
        client_info: dict[str, Any],
        protected_resource_metadata: dict[str, Any] | None,
        oauth_metadata: dict[str, Any] | None,
        redirect_uri: str,
        now_ms: int,
    ) -> CapabilityOsMcpOAuthCredential:
        encrypted_access = self._encrypt(access_token)
        encrypted_refresh = self._encrypt(refresh_token) if refresh_token else None
        encrypted_client = self.encrypt_json(client_info)
        async with self._session_factory() as db:
            row = await db.get(CapabilityOsMcpOAuthCredential, logical_name)
            values = {
                "user_id": user_id,
                "profile_id": profile_id,
                "server_id": server_id,
                "remote_url": remote_url,
                "consumer": consumer,
                "access_token_encrypted": encrypted_access,
                "refresh_token_encrypted": encrypted_refresh,
                "token_type": token_type,
                "scope": scope,
                "expires_at_ms": expires_at_ms,
                "client_info_encrypted": encrypted_client,
                "protected_resource_metadata": protected_resource_metadata,
                "oauth_metadata": oauth_metadata,
                "redirect_uri": redirect_uri,
                "updated_at_ms": int(now_ms),
                "revoked_at_ms": None,
            }
            if row is None:
                row = CapabilityOsMcpOAuthCredential(logical_name=logical_name, **values)
                db.add(row)
            else:
                if (
                    row.user_id != user_id
                    or row.profile_id != profile_id
                    or row.server_id != server_id
                    or row.remote_url != remote_url
                    or row.consumer != consumer
                ):
                    raise McpOAuthError("OAuth logical credential identity collision")
                for key, value in values.items():
                    setattr(row, key, value)
            await db.commit()
            await db.refresh(row)
            return row

    async def finalize_flow_with_credential(
        self,
        flow_id: str,
        *,
        logical_name: str,
        user_id: str,
        profile_id: str,
        server_id: str,
        remote_url: str,
        consumer: str,
        access_token: str,
        refresh_token: str | None,
        token_type: str,
        scope: str | None,
        expires_at_ms: int | None,
        client_info: dict[str, Any],
        protected_resource_metadata: dict[str, Any] | None,
        oauth_metadata: dict[str, Any] | None,
        redirect_uri: str,
        now_ms: int,
    ) -> bool:
        """Atomically publish an exchanged credential and complete its claimed flow."""
        encrypted_access = self._encrypt(access_token)
        encrypted_refresh = self._encrypt(refresh_token) if refresh_token else None
        encrypted_client = self.encrypt_json(client_info)
        async with self._session_factory() as db:
            async with db.begin():
                flow = await db.scalar(
                    select(CapabilityOsMcpOAuthFlow).where(
                        CapabilityOsMcpOAuthFlow.flow_id == str(flow_id),
                        CapabilityOsMcpOAuthFlow.status == "exchanging",
                    )
                )
                if flow is None:
                    return False
                row = await db.get(CapabilityOsMcpOAuthCredential, logical_name)
                values = {
                    "user_id": user_id,
                    "profile_id": profile_id,
                    "server_id": server_id,
                    "remote_url": remote_url,
                    "consumer": consumer,
                    "access_token_encrypted": encrypted_access,
                    "refresh_token_encrypted": encrypted_refresh,
                    "token_type": token_type,
                    "scope": scope,
                    "expires_at_ms": expires_at_ms,
                    "client_info_encrypted": encrypted_client,
                    "protected_resource_metadata": protected_resource_metadata,
                    "oauth_metadata": oauth_metadata,
                    "redirect_uri": redirect_uri,
                    "updated_at_ms": int(now_ms),
                    "revoked_at_ms": None,
                }
                if row is None:
                    db.add(CapabilityOsMcpOAuthCredential(logical_name=logical_name, **values))
                else:
                    if (
                        row.user_id != user_id
                        or row.profile_id != profile_id
                        or row.server_id != server_id
                        or row.remote_url != remote_url
                        or row.consumer != consumer
                    ):
                        raise McpOAuthError("OAuth logical credential identity collision")
                    for key, value in values.items():
                        setattr(row, key, value)
                flow.status = "complete"
                flow.completed_at_ms = int(now_ms)
                flow.error_code = None
        return True

    async def update_tokens(
        self,
        logical_name: str,
        *,
        access_token: str,
        refresh_token: str | None,
        token_type: str,
        scope: str | None,
        expires_at_ms: int | None,
        now_ms: int,
    ) -> CapabilityOsMcpOAuthCredential:
        async with self._session_factory() as db:
            row = await db.get(CapabilityOsMcpOAuthCredential, str(logical_name))
            if row is None or row.revoked_at_ms is not None:
                raise McpOAuthError("MCP OAuth credential is unavailable")
            row.access_token_encrypted = self._encrypt(access_token)
            if refresh_token:
                row.refresh_token_encrypted = self._encrypt(refresh_token)
            row.token_type = token_type
            row.scope = scope
            row.expires_at_ms = expires_at_ms
            row.updated_at_ms = int(now_ms)
            await db.commit()
            await db.refresh(row)
            return row

    async def revoke_credential(self, logical_name: str, *, now_ms: int) -> bool:
        async with self._session_factory() as db:
            result = await db.execute(
                update(CapabilityOsMcpOAuthCredential)
                .where(
                    CapabilityOsMcpOAuthCredential.logical_name == str(logical_name),
                    CapabilityOsMcpOAuthCredential.revoked_at_ms.is_(None),
                )
                .values(revoked_at_ms=int(now_ms))
            )
            await db.commit()
            return bool(result.rowcount)

    def access_token(self, row: CapabilityOsMcpOAuthCredential) -> str:
        return self._decrypt(row.access_token_encrypted)

    def refresh_token(self, row: CapabilityOsMcpOAuthCredential) -> str | None:
        if not row.refresh_token_encrypted:
            return None
        return self._decrypt(row.refresh_token_encrypted)

    def flow_code_verifier(self, row: CapabilityOsMcpOAuthFlow) -> str:
        return self._decrypt(row.code_verifier_encrypted)

    def flow_client_info(self, row: CapabilityOsMcpOAuthFlow) -> dict[str, Any]:
        return self.decrypt_json(row.client_info_encrypted)

    def credential_client_info(self, row: CapabilityOsMcpOAuthCredential) -> dict[str, Any]:
        return self.decrypt_json(row.client_info_encrypted)


class McpOAuthService:
    """Own discovery, DCR/CIMD, PKCE exchange, refresh, and OAuth status."""

    def __init__(
        self,
        *,
        artifacts: SqlCapabilityOsStore,
        oauth_store: McpOAuthStore | None = None,
        profiles: ConfigRemoteMcpOAuthProfileProvider | None = None,
        endpoint_validator: PublicHttpsEndpointValidator | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        timeout_seconds: float = 15.0,
        flow_ttl_ms: int = 10 * 60 * 1000,
    ) -> None:
        if timeout_seconds <= 0 or not 60_000 <= int(flow_ttl_ms) <= 30 * 60 * 1000:
            raise ValueError("MCP OAuth bounds are invalid")
        self._artifacts = artifacts
        self._store = oauth_store or McpOAuthStore()
        self._profiles = profiles or ConfigRemoteMcpOAuthProfileProvider()
        self._validator = endpoint_validator or PublicHttpsEndpointValidator()
        self._clock_ms = clock_ms
        self._timeout_seconds = float(timeout_seconds)
        self._flow_ttl_ms = int(flow_ttl_ms)
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._refresh_lock_guard = asyncio.Lock()

    @staticmethod
    def logical_name(*, user_id: str, profile_id: str, server_id: str, remote_url: str) -> str:
        payload = "\0".join((str(user_id), str(profile_id), str(server_id), str(remote_url)))
        return "mcp.oauth:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]

    @staticmethod
    def credential_consumer(remote_url: str) -> str:
        return f"mcp.remote:{str(remote_url).strip()}"

    async def _http_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "cptr-capability-os-oauth"},
        )

    async def _validated_url(self, value: str) -> str:
        return await self._validator.validate(str(value).strip())

    async def _profile_for_row(self, row, *, profile_id: str) -> RemoteMcpOAuthProfile:
        spec = dict(row.spec or {})
        remote = spec.get("remote") if isinstance(spec.get("remote"), dict) else {}
        server_id = str(spec.get("serverId") or "").strip()
        remote_url = str(remote.get("url") or "").strip()
        if not server_id or remote.get("transport") != "streamable-http" or not remote_url:
            raise McpOAuthError("MCP OAuth adapter identity is incomplete")
        normalized_remote = await self._validated_url(remote_url)
        if normalized_remote != remote_url:
            raise McpOAuthError("MCP OAuth adapter remote URL is not canonical")
        profile = await self._profiles.resolve(
            profile_id=profile_id,
            server_id=server_id,
            remote_url=remote_url,
        )
        if profile is None:
            raise McpOAuthError("MCP OAuth profile is not configured")
        if await self._validated_url(profile.remote_url) != remote_url:
            raise McpOAuthError("MCP OAuth profile remote URL does not match adapter")
        redirect = await self._validated_url(profile.redirect_uri)
        if urlsplit(redirect).path != "/api/oauth/mcp/callback":
            raise McpOAuthError("MCP OAuth redirect URI must target the CPTR callback path")
        if redirect != profile.redirect_uri:
            raise McpOAuthError("MCP OAuth redirect URI is not canonical")
        if profile.client_metadata_url:
            metadata_url = await self._validated_url(profile.client_metadata_url)
            if metadata_url != profile.client_metadata_url:
                raise McpOAuthError("MCP OAuth client metadata URL is not canonical")
        return profile

    async def _bootstrap_challenge(self, remote_url: str) -> httpx.Response:
        payload = {
            "jsonrpc": "2.0",
            "id": "cptr-oauth-bootstrap",
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "cptr-capability-os", "version": "1"},
            },
        }
        async with await self._http_client() as client:
            response = await client.post(
                remote_url,
                json=payload,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": "2025-06-18",
                },
            )
        if response.status_code not in {401, 403}:
            raise McpOAuthError("remote MCP did not issue an OAuth authorization challenge")
        return response

    async def _discover(
        self,
        *,
        remote_url: str,
        challenge: httpx.Response,
    ) -> tuple[Any | None, Any | None, str | None, str]:
        from mcp.client.auth.oauth2 import (
            build_oauth_authorization_server_metadata_discovery_urls,
            build_protected_resource_metadata_discovery_urls,
            check_resource_allowed,
            extract_resource_metadata_from_www_auth,
            extract_scope_from_www_auth,
            get_client_metadata_scopes,
            handle_auth_metadata_response,
            handle_protected_resource_response,
            resource_url_from_server_url,
        )

        resource_metadata_url = extract_resource_metadata_from_www_auth(challenge)
        prm = None
        async with await self._http_client() as client:
            for raw_url in build_protected_resource_metadata_discovery_urls(
                resource_metadata_url, remote_url
            ):
                try:
                    url = await self._validated_url(raw_url)
                    response = await client.get(url, headers={"Accept": "application/json"})
                    prm = await handle_protected_resource_response(response)
                except (httpx.HTTPError, McpOAuthError):
                    prm = None
                if prm is not None:
                    break

            if prm is not None:
                expected_resource = resource_url_from_server_url(remote_url)
                if not check_resource_allowed(
                    requested_resource=expected_resource,
                    configured_resource=str(prm.resource),
                ):
                    raise McpOAuthError("MCP OAuth protected resource metadata does not match remote")
                auth_server_url = str(prm.authorization_servers[0])
            else:
                auth_server_url = None

            oauth_metadata = None
            for raw_url in build_oauth_authorization_server_metadata_discovery_urls(
                auth_server_url, remote_url
            ):
                try:
                    url = await self._validated_url(raw_url)
                    response = await client.get(url, headers={"Accept": "application/json"})
                    keep_trying, metadata = await handle_auth_metadata_response(response)
                except (httpx.HTTPError, McpOAuthError):
                    continue
                if metadata is not None:
                    oauth_metadata = metadata
                    break
                if not keep_trying:
                    break

        scope = get_client_metadata_scopes(
            extract_scope_from_www_auth(challenge), prm, oauth_metadata
        )
        base = auth_server_url or f"{urlsplit(remote_url).scheme}://{urlsplit(remote_url).netloc}"
        return prm, oauth_metadata, scope, base

    async def _client_info(
        self,
        *,
        profile: RemoteMcpOAuthProfile,
        oauth_metadata,
        auth_base_url: str,
        scope: str | None,
        existing: CapabilityOsMcpOAuthCredential | None,
    ):
        from mcp.client.auth.oauth2 import (
            OAuthClientMetadata,
            create_client_info_from_metadata_url,
            create_client_registration_request,
            handle_registration_response,
            should_use_client_metadata_url,
        )
        from mcp.shared.auth import OAuthClientInformationFull

        if existing is not None:
            return OAuthClientInformationFull.model_validate(
                self._store.credential_client_info(existing)
            )

        metadata = OAuthClientMetadata(
            client_name=profile.client_name,
            redirect_uris=[AnyUrl(profile.redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=scope,
        )
        if should_use_client_metadata_url(oauth_metadata, profile.client_metadata_url):
            return create_client_info_from_metadata_url(
                str(profile.client_metadata_url), redirect_uris=metadata.redirect_uris
            )
        registration = create_client_registration_request(
            oauth_metadata, metadata, auth_base_url
        )
        registration_url = await self._validated_url(str(registration.url))
        if registration_url != str(registration.url):
            raise McpOAuthError("MCP OAuth registration endpoint is not canonical")
        async with await self._http_client() as client:
            response = await client.send(registration)
        try:
            return await handle_registration_response(response)
        except Exception as exc:
            raise McpOAuthError("MCP OAuth dynamic client registration failed") from exc

    async def _derived_adapter(
        self,
        row,
        *,
        profile_id: str,
        logical_name: str,
    ):
        spec = dict(row.spec or {})
        remote = dict(spec.get("remote") or {})
        qualified_spec = dict(spec)
        qualified_spec["authentication"] = {
            "mechanism": "bearer",
            "logicalName": logical_name,
            "consumer": self.credential_consumer(str(remote.get("url") or "")),
            "source": "oauth2",
            "profileId": profile_id,
        }
        qualified_spec["qualification"] = {
            "state": "oauth-pending",
            "identity": "registry+endpoint-validation",
            "auth": "server-owned-oauth2",
            "sandbox": "remote-no-local-execution",
        }
        parent = f"{row.artifact_id}@{row.version}#{row.content_digest}"
        artifact = create_artifact(
            artifact_id=row.artifact_id,
            version=row.version,
            kind=ArtifactKind.MCP_ADAPTER,
            owner=ArtifactOwner.EXTERNAL,
            origin=ArtifactOrigin.MCP,
            spec=qualified_spec,
            created_at=_iso_from_ms(int(self._clock_ms())),
            user_id=row.user_id,
            parent=parent,
            task_origin=row.task_origin,
            source_digest=row.source_digest,
            state=ArtifactState.EPHEMERAL,
        )
        return await self._artifacts.persist_artifact(artifact)

    async def start(
        self,
        *,
        user_id: str,
        task_id: str,
        artifact_digest: str,
    ) -> McpOAuthStartResult:
        row = await self._artifacts.get_artifact(
            artifact_digest, user_id=user_id, include_global=False
        )
        if row is None or row.kind != ArtifactKind.MCP_ADAPTER.value:
            raise McpOAuthError("MCP OAuth adapter artifact not found")
        if row.task_origin != task_id or row.state != ArtifactState.EPHEMERAL.value:
            raise McpOAuthError("MCP OAuth requires a task-scoped ephemeral adapter")
        spec = dict(row.spec or {})
        auth = spec.get("authentication") if isinstance(spec.get("authentication"), dict) else {}
        if auth.get("mechanism") != "oauth2":
            raise McpOAuthError("MCP adapter is not awaiting OAuth authorization")
        profile_id = str(auth.get("logicalName") or "").strip()
        if not profile_id:
            raise McpOAuthError("MCP OAuth adapter lacks a server-owned profile")
        profile = await self._profile_for_row(row, profile_id=profile_id)
        logical_name = self.logical_name(
            user_id=user_id,
            profile_id=profile_id,
            server_id=profile.server_id,
            remote_url=profile.remote_url,
        )
        derived = await self._derived_adapter(
            row, profile_id=profile_id, logical_name=logical_name
        )
        existing = await self._store.get_credential(logical_name)
        if existing is not None and existing.revoked_at_ms is None:
            try:
                await self.access_token(logical_name=logical_name, consumer=existing.consumer)
            except McpOAuthError:
                pass
            else:
                return McpOAuthStartResult(
                    flow_id=None,
                    artifact_digest=derived.content_digest,
                    authorization_url=None,
                    status="ready",
                    expires_at_ms=None,
                )

        challenge = await self._bootstrap_challenge(profile.remote_url)
        prm, oauth_metadata, scope, auth_base = await self._discover(
            remote_url=profile.remote_url,
            challenge=challenge,
        )
        client_info = await self._client_info(
            profile=profile,
            oauth_metadata=oauth_metadata,
            auth_base_url=auth_base,
            scope=scope,
            existing=existing,
        )
        from mcp.client.auth.oauth2 import PKCEParameters, resource_url_from_server_url

        pkce = PKCEParameters.generate()
        state = secrets.token_urlsafe(32)
        if oauth_metadata is not None and oauth_metadata.authorization_endpoint:
            authorization_endpoint = str(oauth_metadata.authorization_endpoint)
        else:
            authorization_endpoint = urljoin(auth_base, "/authorize")
        authorization_endpoint = await self._validated_url(authorization_endpoint)
        params = {
            "response_type": "code",
            "client_id": str(client_info.client_id),
            "redirect_uri": profile.redirect_uri,
            "state": state,
            "code_challenge": pkce.code_challenge,
            "code_challenge_method": "S256",
            "resource": (
                str(prm.resource)
                if prm is not None
                else resource_url_from_server_url(profile.remote_url)
            ),
        }
        if scope:
            params["scope"] = scope
        authorization_url = f"{authorization_endpoint}?{urlencode(params)}"
        now = int(self._clock_ms())
        flow = await self._store.create_flow(
            user_id=user_id,
            task_id=task_id,
            artifact_digest=row.content_digest,
            derived_artifact_digest=derived.content_digest,
            server_id=profile.server_id,
            remote_url=profile.remote_url,
            profile_id=profile_id,
            logical_name=logical_name,
            redirect_uri=profile.redirect_uri,
            state=state,
            code_verifier=pkce.code_verifier,
            client_info=client_info.model_dump(mode="json", by_alias=True, exclude_none=True),
            protected_resource_metadata=(
                prm.model_dump(mode="json", by_alias=True, exclude_none=True)
                if prm is not None
                else None
            ),
            oauth_metadata=(
                oauth_metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
                if oauth_metadata is not None
                else None
            ),
            scope=scope,
            now_ms=now,
            expires_at_ms=now + self._flow_ttl_ms,
        )
        return McpOAuthStartResult(
            flow_id=flow.flow_id,
            artifact_digest=derived.content_digest,
            authorization_url=authorization_url,
            status="pending",
            expires_at_ms=flow.expires_at_ms,
        )

    async def status(self, *, user_id: str, task_id: str, flow_id: str) -> dict[str, Any]:
        row = await self._store.get_flow(flow_id)
        if row is None or row.user_id != user_id or row.task_id != task_id:
            raise McpOAuthError("MCP OAuth flow not found")
        now = int(self._clock_ms())
        should_expire = row.status == "pending" and row.expires_at_ms <= now
        if row.status == "exchanging":
            exchange_grace_ms = max(60_000, int(self._timeout_seconds * 4_000))
            should_expire = row.expires_at_ms + exchange_grace_ms <= now
        if should_expire:
            await self._store.set_flow_status(
                row.flow_id,
                expected_status=row.status,
                status="expired",
                now_ms=now,
                error_code="flow-expired",
            )
            row = await self._store.get_flow(flow_id)
            assert row is not None
        return {
            "flowId": row.flow_id,
            "status": row.status,
            "artifactDigest": row.derived_artifact_digest,
            "expiresAtMs": int(row.expires_at_ms),
            "errorCode": row.error_code,
        }

    async def complete_callback(
        self,
        *,
        state: str,
        code: str | None,
        issuer: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        if not state or len(state) > 512:
            raise McpOAuthError("MCP OAuth callback state is invalid")
        flow = await self._store.get_flow_by_state(state)
        if flow is None:
            raise McpOAuthError("MCP OAuth callback state is unknown")
        now = int(self._clock_ms())
        if flow.status != "pending":
            raise McpOAuthError("MCP OAuth flow is no longer pending")
        if flow.expires_at_ms <= now:
            await self._store.set_flow_status(
                flow.flow_id,
                expected_status="pending",
                status="expired",
                now_ms=now,
                error_code="flow-expired",
            )
            raise McpOAuthError("MCP OAuth flow expired")
        if error:
            await self._store.set_flow_status(
                flow.flow_id,
                expected_status="pending",
                status="failed",
                now_ms=now,
                error_code="authorization-denied",
            )
            raise McpOAuthError("MCP OAuth authorization was denied")
        if not code or len(code) > 8192 or any(ord(char) < 32 for char in code):
            raise McpOAuthError("MCP OAuth authorization code is invalid")
        if issuer and flow.oauth_metadata:
            expected = str(flow.oauth_metadata.get("issuer") or "")
            if expected and str(issuer).rstrip("/") != expected.rstrip("/"):
                await self._store.set_flow_status(
                    flow.flow_id,
                    expected_status="pending",
                    status="failed",
                    now_ms=now,
                    error_code="issuer-mismatch",
                )
                raise McpOAuthError("MCP OAuth authorization issuer mismatch")
        claimed = await self._store.set_flow_status(
            flow.flow_id,
            expected_status="pending",
            status="exchanging",
            now_ms=now,
        )
        if not claimed:
            raise McpOAuthError("MCP OAuth flow is no longer pending")
        try:
            token = await self._exchange_code(flow=flow, code=code)
            client_info = self._store.flow_client_info(flow)
            completed_at = int(self._clock_ms())
            finalized = await self._store.finalize_flow_with_credential(
                flow.flow_id,
                logical_name=flow.logical_name,
                user_id=flow.user_id,
                profile_id=flow.profile_id,
                server_id=flow.server_id,
                remote_url=flow.remote_url,
                consumer=self.credential_consumer(flow.remote_url),
                access_token=str(token.access_token),
                refresh_token=(str(token.refresh_token) if token.refresh_token else None),
                token_type=str(token.token_type),
                scope=(str(token.scope) if token.scope else flow.scope),
                expires_at_ms=(
                    completed_at + int(token.expires_in) * 1000
                    if token.expires_in is not None
                    else None
                ),
                client_info=client_info,
                protected_resource_metadata=(
                    dict(flow.protected_resource_metadata)
                    if isinstance(flow.protected_resource_metadata, dict)
                    else None
                ),
                oauth_metadata=(
                    dict(flow.oauth_metadata) if isinstance(flow.oauth_metadata, dict) else None
                ),
                redirect_uri=flow.redirect_uri,
                now_ms=completed_at,
            )
            if not finalized:
                raise McpOAuthError("MCP OAuth flow changed during completion")
        except Exception as exc:
            await self._store.set_flow_status(
                flow.flow_id,
                expected_status="exchanging",
                status="failed",
                now_ms=int(self._clock_ms()),
                error_code=exc.__class__.__name__,
            )
            if isinstance(exc, McpOAuthError):
                raise
            raise McpOAuthError("MCP OAuth token exchange failed") from exc
        return {
            "flowId": flow.flow_id,
            "status": "complete",
            "artifactDigest": flow.derived_artifact_digest,
        }

    async def _exchange_code(self, *, flow: CapabilityOsMcpOAuthFlow, code: str):
        from mcp.client.auth.oauth2 import handle_token_response_scopes, resource_url_from_server_url
        from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata

        client_info = OAuthClientInformationFull.model_validate(self._store.flow_client_info(flow))
        metadata = (
            OAuthMetadata.model_validate(flow.oauth_metadata)
            if isinstance(flow.oauth_metadata, dict)
            else None
        )
        token_url = (
            str(metadata.token_endpoint)
            if metadata is not None and metadata.token_endpoint
            else urljoin(
                f"{urlsplit(flow.remote_url).scheme}://{urlsplit(flow.remote_url).netloc}",
                "/token",
            )
        )
        token_url = await self._validated_url(token_url)
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": flow.redirect_uri,
            "client_id": str(client_info.client_id),
            "code_verifier": self._store.flow_code_verifier(flow),
            "resource": (
                str(flow.protected_resource_metadata.get("resource"))
                if isinstance(flow.protected_resource_metadata, dict)
                and flow.protected_resource_metadata.get("resource")
                else resource_url_from_server_url(flow.remote_url)
            ),
        }
        data, headers = _prepare_client_auth(client_info, data)
        async with await self._http_client() as client:
            response = await client.post(token_url, data=data, headers=headers)
        try:
            return await handle_token_response_scopes(response)
        except Exception as exc:
            raise McpOAuthError("MCP OAuth token endpoint rejected authorization code") from exc
        finally:
            data["code"] = ""
            data["code_verifier"] = ""
            data.pop("client_secret", None)
            headers.pop("Authorization", None)

    async def _refresh_lock(self, logical_name: str) -> asyncio.Lock:
        async with self._refresh_lock_guard:
            lock = self._refresh_locks.get(logical_name)
            if lock is None:
                lock = asyncio.Lock()
                self._refresh_locks[logical_name] = lock
            return lock

    async def access_token(self, *, logical_name: str, consumer: str) -> str:
        row = await self._store.get_credential(logical_name)
        if row is None or row.revoked_at_ms is not None or row.consumer != str(consumer).strip():
            raise McpOAuthError("MCP OAuth credential is unavailable")
        now = int(self._clock_ms())
        if row.expires_at_ms is None or row.expires_at_ms > now + 30_000:
            return self._store.access_token(row)
        lock = await self._refresh_lock(logical_name)
        async with lock:
            row = await self._store.get_credential(logical_name)
            if row is None or row.revoked_at_ms is not None or row.consumer != str(consumer).strip():
                raise McpOAuthError("MCP OAuth credential is unavailable")
            now = int(self._clock_ms())
            if row.expires_at_ms is None or row.expires_at_ms > now + 30_000:
                return self._store.access_token(row)
            refreshed = await self._refresh(row)
            return self._store.access_token(refreshed)

    async def _refresh(
        self, row: CapabilityOsMcpOAuthCredential
    ) -> CapabilityOsMcpOAuthCredential:
        from mcp.client.auth.oauth2 import handle_token_response_scopes, resource_url_from_server_url
        from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata

        refresh_token = self._store.refresh_token(row)
        if not refresh_token:
            raise McpOAuthError("MCP OAuth credential requires reauthorization")
        client_info = OAuthClientInformationFull.model_validate(
            self._store.credential_client_info(row)
        )
        metadata = (
            OAuthMetadata.model_validate(row.oauth_metadata)
            if isinstance(row.oauth_metadata, dict)
            else None
        )
        token_url = (
            str(metadata.token_endpoint)
            if metadata is not None and metadata.token_endpoint
            else urljoin(
                f"{urlsplit(row.remote_url).scheme}://{urlsplit(row.remote_url).netloc}",
                "/token",
            )
        )
        token_url = await self._validated_url(token_url)
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": str(client_info.client_id),
            "resource": (
                str(row.protected_resource_metadata.get("resource"))
                if isinstance(row.protected_resource_metadata, dict)
                and row.protected_resource_metadata.get("resource")
                else resource_url_from_server_url(row.remote_url)
            ),
        }
        data, headers = _prepare_client_auth(client_info, data)
        try:
            async with await self._http_client() as client:
                response = await client.post(token_url, data=data, headers=headers)
            token = await handle_token_response_scopes(response)
        except Exception as exc:
            raise McpOAuthError("MCP OAuth token refresh failed") from exc
        finally:
            data["refresh_token"] = ""
            data.pop("client_secret", None)
            headers.pop("Authorization", None)
        now = int(self._clock_ms())
        return await self._store.update_tokens(
            row.logical_name,
            access_token=str(token.access_token),
            refresh_token=(str(token.refresh_token) if token.refresh_token else None),
            token_type=str(token.token_type),
            scope=(str(token.scope) if token.scope else row.scope),
            expires_at_ms=(
                now + int(token.expires_in) * 1000 if token.expires_in is not None else None
            ),
            now_ms=now,
        )

    async def revoke(self, *, user_id: str, logical_name: str) -> bool:
        row = await self._store.get_credential(logical_name)
        if row is None or row.user_id != user_id:
            return False
        return await self._store.revoke_credential(logical_name, now_ms=int(self._clock_ms()))


class McpOAuthCredentialProvider:
    """CredentialBroker provider backed by encrypted, refreshable OAuth tokens."""

    def __init__(self, service: McpOAuthService) -> None:
        self._service = service

    async def allows(self, *, logical_name: str, consumer: str) -> bool:
        row = await self._service._store.get_credential(str(logical_name).strip())
        return bool(
            row is not None
            and row.revoked_at_ms is None
            and row.consumer == str(consumer).strip()
        )

    async def fetch(
        self,
        *,
        logical_name: str,
        task_id: str,
        lease_id: str,
        consumer: str,
    ) -> str | None:
        del task_id, lease_id
        try:
            return await self._service.access_token(
                logical_name=str(logical_name).strip(),
                consumer=str(consumer).strip(),
            )
        except McpOAuthError:
            return None


def _prepare_client_auth(client_info, data: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    result = dict(data)
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    method = str(client_info.token_endpoint_auth_method or "none")
    client_id = str(client_info.client_id or "")
    secret = str(client_info.client_secret or "")
    if method == "none":
        return result, headers
    if method == "client_secret_post":
        if not secret:
            raise McpOAuthError("MCP OAuth client secret is unavailable")
        result["client_secret"] = secret
        return result, headers
    if method == "client_secret_basic":
        if not secret:
            raise McpOAuthError("MCP OAuth client secret is unavailable")
        encoded_id = quote(client_id, safe="")
        encoded_secret = quote(secret, safe="")
        encoded = base64.b64encode(f"{encoded_id}:{encoded_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {encoded}"
        return result, headers
    raise McpOAuthError("MCP OAuth token endpoint authentication method is unsupported")


def _state_hash(state: str) -> str:
    value = str(state)
    if not value:
        raise McpOAuthError("MCP OAuth state must not be blank")
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso_from_ms(value: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
