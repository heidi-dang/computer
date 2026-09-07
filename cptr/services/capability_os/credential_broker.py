"""Lease-bound logical credential broker for Capability OS.

The broker deliberately has no API that returns a secret.  A caller receives an
opaque handle, and trusted execution code may pass the secret directly into an
in-process consumer callback.  Handle metadata contains no credential value and
task/lease closure can revoke all remaining authority.
"""
from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, TypeVar

from cptr.services.capability_os.authority import CapabilityLease


class CredentialDenied(PermissionError):
    pass


class CredentialUnavailable(RuntimeError):
    pass


class CredentialProvider(Protocol):
    async def fetch(
        self, *, logical_name: str, task_id: str, lease_id: str, consumer: str
    ) -> str | bytes | None: ...


@dataclass(frozen=True)
class CredentialSourceBinding:
    """Server-owned mapping from a logical name to one encrypted CPTR source."""

    logical_name: str
    source_type: str
    source_ref: str
    consumers: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.logical_name.strip() or not self.source_ref.strip():
            raise ValueError("credential source logical name/ref must not be blank")
        if self.source_type not in {"ai_connection", "encrypted_config"}:
            raise ValueError("unsupported credential source type")
        normalized = tuple(dict.fromkeys(str(item).strip() for item in self.consumers if str(item).strip()))
        if not normalized:
            raise ValueError("credential source requires at least one exact consumer")
        object.__setattr__(self, "logical_name", self.logical_name.strip())
        object.__setattr__(self, "source_ref", self.source_ref.strip())
        object.__setattr__(self, "consumers", normalized)


_ALLOWED_ENCRYPTED_CONFIG_KEYS = frozenset({
    "audio.stt_api_key",
    "audio.tts_api_key",
    "images.generation_api_key",
    "images.edit_api_key",
})


class ConfigCredentialProvider:
    """Resolve logical credentials from explicitly configured encrypted CPTR sources.

    The registry contains only source references and exact consumer identities;
    raw credential values remain in the existing encrypted configuration store.
    Plaintext legacy values and arbitrary config-key lookups are rejected.
    """

    def __init__(
        self,
        *,
        config_getter: Callable[[str], Awaitable[Any]] | None = None,
        secret_getter: Callable[[], str] | None = None,
        decryptor: Callable[[str, str], str] | None = None,
        source_config_key: str = "capability_os.credential_sources",
    ) -> None:
        self._config_getter = config_getter
        self._secret_getter = secret_getter
        self._decryptor = decryptor
        self._source_config_key = str(source_config_key).strip()
        if not self._source_config_key:
            raise ValueError("credential source config key must not be blank")

    async def _get_config(self, key: str) -> Any:
        if self._config_getter is not None:
            return await self._config_getter(key)
        from cptr.models import Config
        return await Config.get(key)

    def _decrypt(self, stored: str) -> str | None:
        # Capability OS never upgrades a legacy plaintext config value into a
        # usable credential authority. Operators must first save it through an
        # encrypted product path.
        if not isinstance(stored, str) or not stored.startswith("encrypted:"):
            return None
        if self._secret_getter is None:
            from cptr.utils.config import _get_jwt_secret
            secret = _get_jwt_secret()
        else:
            secret = self._secret_getter()
        if self._decryptor is None:
            from cptr.utils.crypto import decrypt_key
            return decrypt_key(stored, secret)
        return self._decryptor(stored, secret)

    @staticmethod
    def _parse_binding(value: Any) -> CredentialSourceBinding | None:
        if not isinstance(value, dict):
            return None
        try:
            binding = CredentialSourceBinding(
                logical_name=str(value.get("logicalName") or ""),
                source_type=str(value.get("sourceType") or ""),
                source_ref=str(value.get("sourceRef") or ""),
                consumers=tuple(value.get("consumers") or ()),
            )
        except (TypeError, ValueError):
            return None
        if binding.source_type == "encrypted_config" and binding.source_ref not in _ALLOWED_ENCRYPTED_CONFIG_KEYS:
            return None
        return binding

    async def _bindings(self) -> dict[str, CredentialSourceBinding]:
        raw = await self._get_config(self._source_config_key)
        if not isinstance(raw, list):
            return {}
        result: dict[str, CredentialSourceBinding] = {}
        ambiguous: set[str] = set()
        for item in raw[:256]:
            binding = self._parse_binding(item)
            if binding is None:
                continue
            if binding.logical_name in result:
                ambiguous.add(binding.logical_name)
                result.pop(binding.logical_name, None)
                continue
            if binding.logical_name not in ambiguous:
                result[binding.logical_name] = binding
        return result

    async def allows(self, *, logical_name: str, consumer: str) -> bool:
        binding = (await self._bindings()).get(str(logical_name).strip())
        return bool(binding is not None and str(consumer).strip() in binding.consumers)

    async def fetch(
        self, *, logical_name: str, task_id: str, lease_id: str, consumer: str
    ) -> str | bytes | None:
        del task_id, lease_id  # Authority binding is enforced by CredentialBroker.
        bindings = await self._bindings()
        binding = bindings.get(str(logical_name).strip())
        if binding is None or str(consumer).strip() not in binding.consumers:
            return None
        if binding.source_type == "ai_connection":
            connections = await self._get_config("chat.connections")
            if not isinstance(connections, list):
                return None
            matches = [
                item for item in connections
                if isinstance(item, dict) and str(item.get("id") or "") == binding.source_ref
            ]
            if len(matches) != 1 or not matches[0].get("enabled", True):
                return None
            stored = matches[0].get("api_key")
        else:
            stored = await self._get_config(binding.source_ref)
        return self._decrypt(stored) if isinstance(stored, str) else None


class DenyCredentialProvider:
    async def fetch(
        self, *, logical_name: str, task_id: str, lease_id: str, consumer: str
    ) -> None:
        return None


@dataclass(frozen=True)
class CredentialHandle:
    handle_id: str
    task_id: str
    lease_id: str
    logical_name: str
    consumer: str
    issued_at_ms: int
    expires_at_ms: int
    max_uses: int
    uses: int
    status: str


@dataclass
class _HandleState:
    handle_id: str
    task_id: str
    lease_id: str
    logical_name: str
    consumer: str
    issued_at_ms: int
    expires_at_ms: int
    max_uses: int
    uses: int = 0
    status: str = "active"

    def snapshot(self) -> CredentialHandle:
        return CredentialHandle(
            handle_id=self.handle_id,
            task_id=self.task_id,
            lease_id=self.lease_id,
            logical_name=self.logical_name,
            consumer=self.consumer,
            issued_at_ms=self.issued_at_ms,
            expires_at_ms=self.expires_at_ms,
            max_uses=self.max_uses,
            uses=self.uses,
            status=self.status,
        )


T = TypeVar("T")
SecretConsumer = Callable[[str | bytes], T | Awaitable[T]]


class CredentialBroker:
    def __init__(
        self,
        *,
        provider: CredentialProvider | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        max_handle_ttl_ms: int = 60_000,
        max_handles: int = 10_000,
        lease_validator: Callable[[CapabilityLease], bool | Awaitable[bool]] | None = None,
    ) -> None:
        if max_handle_ttl_ms <= 0 or max_handles <= 0:
            raise ValueError("credential broker limits must be positive")
        self._provider = provider or DenyCredentialProvider()
        self._clock_ms = clock_ms
        self._max_handle_ttl_ms = int(max_handle_ttl_ms)
        self._max_handles = int(max_handles)
        self._lease_validator = lease_validator
        self._handles: dict[str, _HandleState] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _allowed_names(lease: CapabilityLease) -> set[str]:
        return {str(item) for item in lease.credentials.get("logicalNames") or () if str(item)}

    async def _lease_active(self, lease: CapabilityLease) -> int:
        now = int(self._clock_ms())
        if lease.status != "active" or lease.expires_at_ms <= now:
            raise CredentialDenied("credential handle requires an active lease")
        if self._lease_validator is not None:
            try:
                valid = self._lease_validator(lease)
                if inspect.isawaitable(valid):
                    valid = await valid
            except Exception as exc:
                raise CredentialDenied("credential lease could not be revalidated") from exc
            if valid is not True:
                raise CredentialDenied("credential lease is no longer active")
        return now

    def _prune_locked(self, now_ms: int) -> None:
        for handle_id, state in list(self._handles.items()):
            if state.status != "active" or state.expires_at_ms <= now_ms:
                self._handles.pop(handle_id, None)

    async def issue(
        self,
        *,
        lease: CapabilityLease,
        logical_name: str,
        consumer: str,
        ttl_ms: int = 30_000,
        max_uses: int = 1,
    ) -> CredentialHandle:
        now = await self._lease_active(lease)
        name = str(logical_name).strip()
        target = str(consumer).strip()
        if not name or not target:
            raise CredentialDenied("logical credential name and consumer must not be blank")
        if name not in self._allowed_names(lease):
            raise CredentialDenied("logical credential is outside the lease")
        authorizer = getattr(self._provider, "allows", None)
        if callable(authorizer):
            try:
                allowed = authorizer(logical_name=name, consumer=target)
                if inspect.isawaitable(allowed):
                    allowed = await allowed
            except Exception as exc:
                raise CredentialDenied("logical credential source cannot authorize this consumer") from exc
            if allowed is not True:
                raise CredentialDenied("logical credential is not authorized for this consumer")
        requested_ttl = int(ttl_ms)
        requested_uses = int(max_uses)
        if requested_ttl <= 0 or requested_uses <= 0:
            raise CredentialDenied("credential handle limits must be positive")
        expires = min(
            lease.expires_at_ms,
            now + min(requested_ttl, self._max_handle_ttl_ms),
        )
        if expires <= now:
            raise CredentialDenied("credential handle would already be expired")
        state = _HandleState(
            handle_id=f"credh_{uuid.uuid4().hex}",
            task_id=lease.task_id,
            lease_id=lease.lease_id,
            logical_name=name,
            consumer=target,
            issued_at_ms=now,
            expires_at_ms=expires,
            max_uses=requested_uses,
        )
        async with self._lock:
            self._prune_locked(now)
            if len(self._handles) >= self._max_handles:
                raise CredentialDenied("credential handle capacity reached")
            self._handles[state.handle_id] = state
        return state.snapshot()

    async def inspect(self, handle_id: str) -> CredentialHandle | None:
        now = int(self._clock_ms())
        async with self._lock:
            state = self._handles.get(str(handle_id))
            if state is None:
                return None
            if state.status == "active" and state.expires_at_ms <= now:
                state.status = "expired"
            return state.snapshot()

    async def use(
        self,
        *,
        handle_id: str,
        lease: CapabilityLease,
        consumer: str,
        operation: SecretConsumer[T],
    ) -> T:
        now = await self._lease_active(lease)
        target = str(consumer).strip()
        if not target:
            raise CredentialDenied("credential consumer must not be blank")
        async with self._lock:
            state = self._handles.get(str(handle_id))
            if state is None:
                raise CredentialDenied("credential handle not found")
            if state.status != "active" or state.expires_at_ms <= now:
                state.status = "expired" if state.expires_at_ms <= now else state.status
                raise CredentialDenied("credential handle is inactive")
            if state.lease_id != lease.lease_id or state.task_id != lease.task_id:
                raise CredentialDenied("credential handle belongs to another lease")
            if state.consumer != target:
                raise CredentialDenied("credential handle belongs to another consumer")
            if state.logical_name not in self._allowed_names(lease):
                raise CredentialDenied("credential authority was removed from the lease")
            if state.uses >= state.max_uses:
                state.status = "consumed"
                raise CredentialDenied("credential handle use budget exhausted")
            # Reserve a use before fetching the secret so concurrent consumers can
            # never exceed the handle budget.  A failed provider call consumes the
            # reservation; fail-closed retries require a fresh handle.
            state.uses += 1
            one_shot_complete = state.uses >= state.max_uses
            if one_shot_complete:
                state.status = "consumed"
            logical_name = state.logical_name

        try:
            secret = await self._provider.fetch(
                logical_name=logical_name,
                task_id=lease.task_id,
                lease_id=lease.lease_id,
                consumer=target,
            )
        except Exception as exc:
            raise CredentialUnavailable("credential provider failed") from exc
        if secret is None or secret == "" or secret == b"":
            raise CredentialUnavailable("logical credential is unavailable")
        try:
            result = operation(secret)
            if inspect.isawaitable(result):
                result = await result
            return result  # type: ignore[return-value]
        finally:
            # The local variable is deliberately dropped immediately after the
            # trusted consumer returns.  Python cannot guarantee memory zeroing,
            # but the broker never stores the secret in handle state or evidence.
            secret = None

    async def inject(
        self,
        *,
        lease: CapabilityLease,
        logical_name: str,
        consumer: str,
        operation: SecretConsumer[T],
        ttl_ms: int = 30_000,
    ) -> T:
        """Inject one secret into one trusted callback without returning a handle."""
        handle = await self.issue(
            lease=lease,
            logical_name=logical_name,
            consumer=consumer,
            ttl_ms=ttl_ms,
            max_uses=1,
        )
        try:
            return await self.use(
                handle_id=handle.handle_id,
                lease=lease,
                consumer=consumer,
                operation=operation,
            )
        finally:
            # A successful/failed use normally marks a one-shot handle consumed;
            # revoke defensively if execution exits before the reservation occurs.
            await self.revoke(handle.handle_id)

    async def revoke(self, handle_id: str) -> bool:
        async with self._lock:
            state = self._handles.get(str(handle_id))
            if state is None or state.status != "active":
                return False
            state.status = "revoked"
            return True

    async def revoke_lease(self, lease_id: str) -> int:
        count = 0
        async with self._lock:
            for state in self._handles.values():
                if state.lease_id == lease_id and state.status == "active":
                    state.status = "revoked"
                    count += 1
        return count

    async def revoke_task(self, task_id: str) -> int:
        count = 0
        async with self._lock:
            for state in self._handles.values():
                if state.task_id == task_id and state.status == "active":
                    state.status = "revoked"
                    count += 1
        return count
