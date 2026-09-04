"""Trust-gated, provider-neutral capability execution for the Dark Factory.

The router is the final server-side policy boundary before a selected capability
may invoke an execution provider. It validates current trust, manifest and
request permissions, network policy, provider identity, SSH alias policy, and
optional terminal-plugin policy before the provider object is touched. Provider
outputs are normalized into bounded redacted machine evidence.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from cptr.services.factory_capabilities import CapabilityManifest, CapabilityTrustStatus
from cptr.utils.redaction import redact_sensitive


class FactoryExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _normalized_permissions(values: Iterable[str]) -> tuple[str, ...]:
    normalized: set[str] = set()
    for raw in values:
        value = str(raw).strip().lower()
        if not value:
            raise ValueError("execution permission must not be blank")
        normalized.add(value)
    return tuple(sorted(normalized))


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth == 0:
        value = redact_sensitive(value)
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:100_000]
    if isinstance(value, dict):
        blocked = {
            "authorization",
            "cookie",
            "credential",
            "env",
            "header",
            "password",
            "private_key",
            "secret",
            "token",
        }
        result: dict[str, Any] = {}
        for key, item in list(value.items())[:500]:
            key_text = str(key)[:160]
            if any(fragment in key_text.lower() for fragment in blocked):
                continue
            result[key_text] = _safe_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, depth=depth + 1) for item in list(value)[:500]]
    return str(value)[:10_000]


def _encoded(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _bounded_output(value: Any, max_bytes: int) -> tuple[Any, str, int, bool]:
    safe = _safe_value(value)
    raw = _encoded(safe)
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) <= max_bytes:
        return safe, digest, len(raw), False
    preview = raw[:max_bytes].decode("utf-8", errors="ignore")
    return (
        {
            "preview": preview,
            "truncated": True,
            "original_bytes": len(raw),
        },
        digest,
        len(raw),
        True,
    )


def _bounded_metadata(value: Any, max_bytes: int = 8192) -> dict[str, Any]:
    safe = _safe_value(value)
    if not isinstance(safe, dict):
        safe = {"value": safe}
    raw = _encoded(safe)
    if len(raw) <= max_bytes:
        return safe
    return {
        "truncated": True,
        "preview": raw[:max_bytes].decode("utf-8", errors="ignore"),
        "digest": hashlib.sha256(raw).hexdigest(),
    }


@dataclass(frozen=True)
class ExecutionPolicy:
    allowed_permissions: frozenset[str]
    network_allowed: bool
    allowed_ssh_aliases: frozenset[str] = frozenset()
    allow_terminal_plugin: bool = False
    max_output_bytes: int = 64 * 1024
    max_runtime_ms: int = 30_000

    def __post_init__(self) -> None:
        permissions = frozenset(_normalized_permissions(self.allowed_permissions))
        aliases = frozenset(
            str(item).strip() for item in self.allowed_ssh_aliases if str(item).strip()
        )
        object.__setattr__(self, "allowed_permissions", permissions)
        object.__setattr__(self, "allowed_ssh_aliases", aliases)
        if self.max_output_bytes <= 0 or self.max_runtime_ms <= 0:
            raise ValueError("execution output/runtime bounds must be positive")


@dataclass(frozen=True)
class ExecutionRequest:
    run_id: str
    cycle_id: str
    manifest: CapabilityManifest
    operation: str
    arguments: dict[str, Any]
    required_permissions: tuple[str, ...] = ()
    network_required: bool = False

    def __post_init__(self) -> None:
        for name in ("run_id", "cycle_id", "operation"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} must not be blank")
        if not isinstance(self.arguments, dict):
            raise TypeError("execution arguments must be an object")
        if len(_encoded(self.arguments)) > 64 * 1024:
            raise ValueError("execution arguments exceed the bounded request size")
        object.__setattr__(
            self,
            "required_permissions",
            _normalized_permissions(self.required_permissions),
        )


@dataclass(frozen=True)
class ProviderExecutionResult:
    output: Any
    metadata: dict[str, Any] = field(default_factory=dict)


class ExecutionProviderAdapter:
    """Adapt a bounded CPTR/provider callable to the factory provider contract."""

    def __init__(
        self,
        *,
        name: str,
        handler: Callable[[ExecutionRequest], Any],
        allowed_operations: Iterable[str] = (),
    ) -> None:
        normalized_name = name.strip().lower()
        if not normalized_name:
            raise ValueError("execution provider name must not be blank")
        if not callable(handler):
            raise TypeError("execution provider handler must be callable")
        async_handler = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
            getattr(handler, "__call__", None)
        )
        if not async_handler:
            raise TypeError(
                "execution provider handler must be async so runtime cancellation is enforceable"
            )
        self.name = normalized_name
        self._handler = handler
        self._allowed_operations = frozenset(
            str(item).strip() for item in allowed_operations if str(item).strip()
        )

    async def execute(self, request: ExecutionRequest) -> ProviderExecutionResult:
        if self._allowed_operations and request.operation not in self._allowed_operations:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_OPERATION_REJECTED",
                f"provider {self.name} does not allow operation {request.operation}",
            )
        result = await self._handler(request)
        if not isinstance(result, ProviderExecutionResult):
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_INVALID_PROVIDER_RESULT",
                f"provider {self.name} returned an invalid result",
            )
        return result


@dataclass(frozen=True)
class ExecutionEvidence:
    evidence_id: str
    run_id: str
    cycle_id: str
    stable_id: str
    capability_identity: str
    provider: str
    operation: str
    status: str
    output: Any
    metadata: dict[str, Any]
    output_digest: str
    output_bytes: int
    truncated: bool
    runtime_ms: int
    started_at_ms: int
    finished_at_ms: int


_REQUIREMENT_PROVIDER = {
    "cptr-direct-coding": "cptr",
    "git-service": "cptr",
    "fdx": "fdx",
    "lsp": "lsp",
    "command-service": "command",
    "managed-browser": "browser",
    "web-search": "browser",
    "mcp-client": "mcp",
    "mcp-stdio-client": "mcp",
    "ssh-configured": "ssh",
    "ssh-service": "ssh",
    "terminal-plugin": "terminal",
}


class FactoryExecutionRouter:
    def __init__(self, *, providers: Mapping[str, ExecutionProviderAdapter]) -> None:
        normalized: dict[str, ExecutionProviderAdapter] = {}
        for key, provider in providers.items():
            name = str(key).strip().lower()
            if not name or provider.name != name:
                raise ValueError("execution provider registry key must match adapter name")
            normalized[name] = provider
        self._providers = normalized

    @staticmethod
    def _provider_name(request: ExecutionRequest, policy: ExecutionPolicy) -> str:
        manifest = request.manifest
        if manifest.trust_status is not CapabilityTrustStatus.APPROVED:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_TRUST_REJECTED",
                f"capability trust state {manifest.trust_status.value} is not executable",
            )

        manifest_permissions = set(_normalized_permissions(manifest.permissions))
        if not manifest_permissions.issubset(policy.allowed_permissions):
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PERMISSION_REJECTED",
                "capability manifest requests permissions outside execution policy",
            )
        requested_permissions = set(request.required_permissions)
        if not requested_permissions.issubset(manifest_permissions):
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PERMISSION_ESCALATION",
                "execution request asks for permissions not granted by the capability manifest",
            )
        if not requested_permissions.issubset(policy.allowed_permissions):
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PERMISSION_REJECTED",
                "execution request asks for permissions outside execution policy",
            )

        if manifest.network_requirements and not policy.network_allowed:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_NETWORK_REJECTED",
                "capability requires network access but execution policy denies it",
            )
        if request.network_required:
            if not policy.network_allowed:
                raise FactoryExecutionError(
                    "FACTORY_EXECUTION_NETWORK_REJECTED",
                    "execution request requires network access but policy denies it",
                )
            if "network:http" not in manifest_permissions:
                raise FactoryExecutionError(
                    "FACTORY_EXECUTION_PERMISSION_ESCALATION",
                    "network execution was requested without manifest network permission",
                )

        requirements = tuple(
            sorted(
                {
                    str(item).strip().lower()
                    for item in manifest.execution_requirements
                    if str(item).strip()
                }
            )
        )
        if not requirements:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PROVIDER_UNAVAILABLE",
                "capability has no execution provider requirement",
            )
        unknown = [item for item in requirements if item not in _REQUIREMENT_PROVIDER]
        if unknown:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PROVIDER_UNAVAILABLE",
                f"unsupported execution requirement: {unknown[0]}",
            )
        provider_names = {_REQUIREMENT_PROVIDER[item] for item in requirements}
        if len(provider_names) != 1:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PROVIDER_AMBIGUOUS",
                "capability maps to multiple execution providers",
            )
        provider_name = next(iter(provider_names))

        if provider_name == "terminal" and not policy.allow_terminal_plugin:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_TERMINAL_DISABLED",
                "optional terminal-plugin execution is disabled by policy",
            )
        if provider_name == "ssh":
            alias = str(request.arguments.get("alias") or "").strip()
            if not alias or alias not in policy.allowed_ssh_aliases:
                raise FactoryExecutionError(
                    "FACTORY_EXECUTION_SSH_ALIAS_REJECTED",
                    "SSH execution requires an explicitly allowed configured alias",
                )
        return provider_name

    async def execute(
        self,
        request: ExecutionRequest,
        policy: ExecutionPolicy,
    ) -> ExecutionEvidence:
        provider_name = self._provider_name(request, policy)
        provider = self._providers.get(provider_name)
        if provider is None:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PROVIDER_UNAVAILABLE",
                f"execution provider {provider_name} is not configured",
            )

        started_at_ms = int(time.time() * 1000)
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                provider.execute(request),
                timeout=policy.max_runtime_ms / 1000,
            )
        except asyncio.TimeoutError as exc:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PROVIDER_TIMEOUT",
                f"execution provider {provider_name} exceeded the runtime bound",
            ) from exc
        except FactoryExecutionError:
            raise
        except Exception as exc:
            raise FactoryExecutionError(
                "FACTORY_EXECUTION_PROVIDER_FAILURE",
                f"execution provider {provider_name} failed with {exc.__class__.__name__}",
            ) from exc

        runtime_ms = int((time.monotonic() - started) * 1000)
        finished_at_ms = int(time.time() * 1000)
        output, digest, output_bytes, truncated = _bounded_output(
            result.output,
            policy.max_output_bytes,
        )
        metadata = _bounded_metadata(result.metadata)
        return ExecutionEvidence(
            evidence_id=f"fexec_{uuid.uuid4().hex}",
            run_id=request.run_id,
            cycle_id=request.cycle_id,
            stable_id=request.manifest.stable_id,
            capability_identity=request.manifest.identity,
            provider=provider_name,
            operation=request.operation,
            status="PASS",
            output=output,
            metadata=metadata,
            output_digest=digest,
            output_bytes=output_bytes,
            truncated=truncated,
            runtime_ms=runtime_ms,
            started_at_ms=started_at_ms,
            finished_at_ms=finished_at_ms,
        )
