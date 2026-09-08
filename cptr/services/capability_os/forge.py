"""Tool Forge lifecycle with content-addressed source and isolated-build enforcement."""

from __future__ import annotations

import datetime as dt
import inspect
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from cptr.services.capability_os.contracts import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactOrigin,
    ArtifactOwner,
    ArtifactState,
    CapabilityRequest,
    CptrArtifact,
    canonical_json,
    digest_payload,
    create_artifact,
)
from cptr.services.capability_os.authority import permission_covers
from cptr.services.capability_os.provenance import (
    build_slsa_v1_statement,
    build_spdx_23_document,
    supply_chain_reference,
    validate_supply_chain_references,
)
from cptr.services.capability_os.runtime import RuntimeBroker, RuntimeClass, RuntimeUnavailable
from cptr.services.capability_os.store import SqlCapabilityOsStore


@dataclass(frozen=True)
class CreateToolRequest:
    tool_id: str
    version: str
    task_id: str
    runtime_class: str
    entrypoint: str
    files: dict[str, str]
    requested_capabilities: tuple[CapabilityRequest, ...]
    user_id: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    deterministic: bool | None = None
    idempotent: bool | None = None
    reversibility: str = "unknown"

    def __post_init__(self) -> None:
        if not self.tool_id.strip() or not self.version.strip() or not self.task_id.strip():
            raise ValueError("tool id, version, and task id must not be blank")
        RuntimeClass(self.runtime_class)
        if not self.entrypoint.strip():
            raise ValueError("tool entrypoint must not be blank")
        if self.entrypoint not in self.files:
            raise ValueError("tool entrypoint must exist in source files")
        if not self.requested_capabilities:
            raise ValueError("tool must declare requested capabilities")
        if self.reversibility not in {"full", "compensating", "partial", "irreversible", "unknown"}:
            raise ValueError("invalid tool reversibility")


@dataclass(frozen=True)
class ToolDraft:
    artifact: CptrArtifact[Any]
    source_digest: str
    source_uri: str


@dataclass(frozen=True)
class ToolBuildResult:
    build_id: str
    source_digest: str
    runtime_class: str
    artifact_digest: str
    attestation: dict[str, Any]
    supply_chain: dict[str, dict[str, str]]


@dataclass(frozen=True)
class ToolRunResult:
    run_id: str
    source_digest: str
    runtime_class: str
    execution_artifact_digest: str
    output: Any
    attestation: dict[str, Any]


class ContentAddressedBlobStore:
    def __init__(self, root: Path, *, max_bytes: int = 4 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        if self.max_bytes <= 0:
            raise ValueError("blob max_bytes must be positive")

    @staticmethod
    def _validate_files(files: dict[str, str]) -> dict[str, str]:
        if not files:
            raise ValueError("tool source must contain at least one file")
        normalized: dict[str, str] = {}
        for raw_path, content in files.items():
            path = PurePosixPath(str(raw_path).replace("\\", "/"))
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError("tool source path must be a safe relative path")
            key = path.as_posix()
            if key.startswith(".") and key in {".", ".."}:
                raise ValueError("invalid tool source path")
            if not isinstance(content, str):
                raise TypeError("tool source content must be UTF-8 text")
            normalized[key] = content
        return dict(sorted(normalized.items()))

    def put_json(self, payload: Any) -> tuple[str, str]:
        raw = canonical_json(payload)
        if len(raw) > self.max_bytes:
            raise ValueError("content-store document exceeds byte bound")
        digest = digest_payload(payload)
        hex_digest = digest.split(":", 1)[1]
        directory = self.root / "sha256"
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(directory, 0o700)
        except OSError:
            pass
        path = directory / f"{hex_digest}.json"
        if not path.exists():
            temp = directory / f".{hex_digest}.{uuid.uuid4().hex}.tmp"
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb", closefd=True) as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if temp.exists():
                    temp.unlink(missing_ok=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return digest, str(path)

    def get_json(self, digest: str) -> Any:
        if not digest.startswith("sha256:"):
            raise ValueError("invalid content-store digest")
        path = self.root / "sha256" / f"{digest.split(':', 1)[1]}.json"
        raw = path.read_bytes()
        if len(raw) > self.max_bytes:
            raise ValueError("stored content-store document exceeds byte bound")
        parsed = json.loads(raw)
        if digest_payload(parsed) != digest:
            raise ValueError("stored content-store document failed digest verification")
        return parsed

    def put_files(self, files: dict[str, str]) -> tuple[str, str]:
        safe = self._validate_files(files)
        return self.put_json({"files": safe})

    def get_files(self, digest: str) -> dict[str, str]:
        parsed = self.get_json(digest)
        files = parsed.get("files") if isinstance(parsed, dict) else None
        if not isinstance(files, dict):
            raise ValueError("stored tool source failed shape verification")
        return {str(key): str(value) for key, value in files.items()}

    def uri(self, digest: str) -> str:
        return str(self.root / "sha256" / f"{digest.split(':', 1)[1]}.json")


class ToolForge:
    def __init__(
        self,
        *,
        store: SqlCapabilityOsStore,
        blobs: ContentAddressedBlobStore,
        runtime: RuntimeBroker,
        builder: Callable[..., Any] | None = None,
        runner: Callable[..., Any] | None = None,
        clock_ms=lambda: int(time.time() * 1000),
    ) -> None:
        self._store = store
        self._blobs = blobs
        self._runtime = runtime
        self._builder = builder
        self._runner = runner
        self._clock_ms = clock_ms

    @staticmethod
    def _iso_now() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")

    async def create(self, request: CreateToolRequest, *, parent: str | None = None) -> ToolDraft:
        source_digest, source_uri = self._blobs.put_files(request.files)
        spec = {
            "inputSchema": request.input_schema,
            "outputSchema": request.output_schema,
            "runtime": {
                "class": request.runtime_class,
                "entrypoint": request.entrypoint,
            },
            "requestedCapabilities": [item.to_dict() for item in request.requested_capabilities],
            "resources": dict(request.resources),
            "semantics": {
                "deterministic": request.deterministic,
                "idempotent": request.idempotent,
                "reversibility": request.reversibility,
            },
            "retention": {"default": "task"},
            "source": {"digest": source_digest, "uri": source_uri},
        }
        artifact = create_artifact(
            artifact_id=request.tool_id,
            version=request.version,
            kind=ArtifactKind.TOOL,
            owner=ArtifactOwner.GENERATED,
            origin=ArtifactOrigin.FORGE,
            spec=spec,
            created_at=self._iso_now(),
            user_id=request.user_id,
            parent=parent,
            task_origin=request.task_id,
            source_digest=source_digest,
        )
        await self._store.persist_artifact(artifact)
        return ToolDraft(artifact=artifact, source_digest=source_digest, source_uri=source_uri)

    async def inspect(self, content_digest: str):
        row = await self._store.get_artifact(content_digest)
        if row is None or row.kind != ArtifactKind.TOOL.value:
            raise KeyError("tool artifact not found")
        return row

    def get_supply_chain_document(self, document_digest: str) -> dict[str, Any]:
        document = self._blobs.get_json(document_digest)
        if not isinstance(document, dict):
            raise ValueError("supply-chain document must be a JSON object")
        return document

    async def modify(
        self,
        content_digest: str,
        *,
        version: str,
        files: dict[str, str],
    ) -> ToolDraft:
        row = await self.inspect(content_digest)
        parent = self._artifact_from_row(row)
        runtime = dict(row.spec.get("runtime") or {})
        requested = tuple(
            CapabilityRequest.from_dict(item) for item in row.spec.get("requestedCapabilities") or []
        )
        return await self.create(
            CreateToolRequest(
                tool_id=row.artifact_id,
                version=version,
                task_id=row.task_origin or "unknown-task",
                runtime_class=str(runtime.get("class") or "gvisor"),
                entrypoint=str(runtime.get("entrypoint") or ""),
                files=files,
                requested_capabilities=requested,
                user_id=row.user_id,
                input_schema=dict(row.spec.get("inputSchema") or {}),
                output_schema=dict(row.spec.get("outputSchema") or {}),
                resources=dict(row.spec.get("resources") or {}),
                deterministic=(row.spec.get("semantics") or {}).get("deterministic"),
                idempotent=(row.spec.get("semantics") or {}).get("idempotent"),
                reversibility=str((row.spec.get("semantics") or {}).get("reversibility") or "unknown"),
            ),
            parent=parent.identity,
        )

    async def fork(
        self,
        content_digest: str,
        *,
        tool_id: str,
        version: str,
    ) -> ToolDraft:
        row = await self.inspect(content_digest)
        parent = self._artifact_from_row(row)
        source = dict(row.spec.get("source") or {})
        files = self._blobs.get_files(str(source.get("digest") or ""))
        runtime = dict(row.spec.get("runtime") or {})
        requested = tuple(
            CapabilityRequest.from_dict(item) for item in row.spec.get("requestedCapabilities") or []
        )
        return await self.create(
            CreateToolRequest(
                tool_id=tool_id,
                version=version,
                task_id=row.task_origin or "unknown-task",
                runtime_class=str(runtime.get("class") or "gvisor"),
                entrypoint=str(runtime.get("entrypoint") or ""),
                files=files,
                requested_capabilities=requested,
                user_id=row.user_id,
                input_schema=dict(row.spec.get("inputSchema") or {}),
                output_schema=dict(row.spec.get("outputSchema") or {}),
                resources=dict(row.spec.get("resources") or {}),
                deterministic=(row.spec.get("semantics") or {}).get("deterministic"),
                idempotent=(row.spec.get("semantics") or {}).get("idempotent"),
                reversibility=str((row.spec.get("semantics") or {}).get("reversibility") or "unknown"),
            ),
            parent=parent.identity,
        )

    async def build(self, content_digest: str, *, lease=None) -> ToolBuildResult:
        row = await self.inspect(content_digest)
        runtime_class = RuntimeClass(str((row.spec.get("runtime") or {}).get("class") or ""))
        if self._builder is None:
            self._runtime.require(runtime_class)
            raise RuntimeUnavailable("no isolated Tool Forge builder adapter is configured")
        if lease is None:
            raise PermissionError("broker-backed Tool Forge build requires an active capability lease")
        if getattr(lease, "artifact_digest", None) != content_digest:
            raise PermissionError("Tool Forge build lease belongs to another artifact")
        if getattr(lease, "task_id", None) != row.task_origin:
            raise PermissionError("Tool Forge build lease belongs to another task")
        if getattr(lease, "runtime_profile", None) != runtime_class.value:
            raise PermissionError("Tool Forge build lease has the wrong runtime profile")
        required_build = CapabilityRequest("runtime.build", f"artifact:{content_digest}")
        if not any(permission_covers(allowed, required_build) for allowed in getattr(lease, "permissions", ())):
            raise PermissionError("Tool Forge build lease lacks runtime.build permission")
        result = self._builder(runtime_class=runtime_class, artifact=row, lease=lease)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise TypeError("isolated builder returned an invalid result")
        artifact_digest = str(result.get("artifact_digest") or "")
        attestation = result.get("attestation")
        if not artifact_digest.startswith("sha256:") or not isinstance(attestation, dict):
            raise ValueError("isolated builder must return artifact digest and attestation")
        build_id = f"build_{uuid.uuid4().hex}"
        source_digest = str(row.source_digest or "")
        source_files = self._blobs.get_files(source_digest)
        sbom = build_spdx_23_document(
            tool_id=row.artifact_id,
            version=row.version,
            source_digest=source_digest,
            files=source_files,
            created_at=self._iso_now(),
        )
        sbom_digest, _sbom_uri = self._blobs.put_json(sbom)
        runtime_spec = dict(row.spec.get("runtime") or {})
        provenance = build_slsa_v1_statement(
            tool_id=row.artifact_id,
            version=row.version,
            build_id=build_id,
            source_digest=source_digest,
            artifact_digest=artifact_digest,
            runtime_class=runtime_class.value,
            entrypoint=str(runtime_spec.get("entrypoint") or ""),
            requested_capabilities=list(row.spec.get("requestedCapabilities") or []),
            broker_attestation=dict(attestation),
            sbom_digest=sbom_digest,
        )
        provenance_digest, _provenance_uri = self._blobs.put_json(provenance)
        supply_chain = {
            "sbom": supply_chain_reference(kind="sbom", digest=sbom_digest),
            "provenance": supply_chain_reference(kind="provenance", digest=provenance_digest),
        }
        return ToolBuildResult(
            build_id=build_id,
            source_digest=source_digest,
            runtime_class=runtime_class.value,
            artifact_digest=artifact_digest,
            attestation=dict(attestation),
            supply_chain=supply_chain,
        )

    async def run(
        self,
        content_digest: str,
        *,
        inputs: dict[str, Any],
        lease=None,
        timeout_ms: int | None = None,
    ) -> ToolRunResult:
        row = await self.inspect(content_digest)
        if ArtifactState(row.state) not in {
            ArtifactState.QUALIFIED,
            ArtifactState.LEARNED,
            ArtifactState.CERTIFIED,
            ArtifactState.CORE,
        }:
            raise PermissionError("generated Tool must be qualified before execution")
        runtime_class = RuntimeClass(str((row.spec.get("runtime") or {}).get("class") or ""))
        if self._runner is None:
            self._runtime.require(runtime_class)
            raise RuntimeUnavailable("no isolated Tool Forge runner adapter is configured")
        if lease is None:
            raise PermissionError("broker-backed Tool execution requires an active capability lease")
        if getattr(lease, "artifact_digest", None) != content_digest:
            raise PermissionError("Tool execution lease belongs to another artifact")
        execution_context = dict(getattr(lease, "execution_context", {}) or {})
        if row.user_id is not None and execution_context.get("userId") != row.user_id:
            raise PermissionError("Tool execution lease belongs to another user")
        if getattr(lease, "runtime_profile", None) != runtime_class.value:
            raise PermissionError("Tool execution lease has the wrong runtime profile")
        required_run = CapabilityRequest("runtime.run", f"artifact:{content_digest}")
        if not any(permission_covers(allowed, required_run) for allowed in getattr(lease, "permissions", ())):
            raise PermissionError("Tool execution lease lacks runtime.run permission")
        if not isinstance(inputs, dict):
            raise TypeError("Tool execution inputs must be an object")
        resources = dict(row.spec.get("resources") or {})
        requested_timeout = int(timeout_ms if timeout_ms is not None else resources.get("wallTimeMs") or 0)
        if requested_timeout <= 0:
            raise ValueError("Tool execution requires a positive timeout")
        result = self._runner(
            runtime_class=runtime_class,
            artifact=row,
            lease=lease,
            inputs=dict(inputs),
            timeout_ms=requested_timeout,
        )
        if inspect.isawaitable(result):
            result = await result
        metadata = getattr(result, "metadata", None)
        if not isinstance(metadata, dict):
            raise TypeError("isolated Tool runner returned an invalid result")
        execution_digest = str(metadata.get("executionArtifactDigest") or "")
        attestation = metadata.get("attestation")
        if not execution_digest.startswith("sha256:") or not isinstance(attestation, dict):
            raise ValueError("isolated Tool runner must return execution digest and attestation")
        return ToolRunResult(
            run_id=f"toolrun_{uuid.uuid4().hex}",
            source_digest=str(row.source_digest),
            runtime_class=runtime_class.value,
            execution_artifact_digest=execution_digest,
            output=getattr(result, "output", None),
            attestation=dict(attestation),
        )

    async def persist(
        self,
        content_digest: str,
        *,
        target_state: ArtifactState,
        evidence_ids: tuple[str, ...],
    ) -> None:
        if target_state not in {
            ArtifactState.QUALIFIED,
            ArtifactState.LEARNED,
            ArtifactState.CERTIFIED,
        }:
            raise ValueError("Tool Forge cannot directly promote to requested state")
        if not evidence_ids:
            raise ValueError("artifact promotion requires evidence")
        row = await self.inspect(content_digest)
        current = ArtifactState(row.state)
        allowed = {
            ArtifactState.EPHEMERAL: {ArtifactState.QUALIFIED},
            ArtifactState.QUALIFIED: {ArtifactState.LEARNED, ArtifactState.CERTIFIED},
            ArtifactState.LEARNED: {ArtifactState.CERTIFIED},
        }
        if target_state not in allowed.get(current, set()):
            raise ValueError("invalid artifact promotion transition")

        evidence_rows = []
        for evidence_id in evidence_ids:
            evidence = await self._store.get_evidence(evidence_id)
            if evidence is None:
                raise ValueError("artifact promotion references unknown evidence")
            if evidence.artifact_digest != content_digest:
                raise ValueError("artifact promotion evidence belongs to another artifact")
            if row.task_origin is not None and evidence.task_id != row.task_origin:
                raise ValueError("artifact promotion evidence belongs to another task")
            if evidence.producer_identity != "capability-os-control":
                raise PermissionError("artifact promotion requires server-produced trusted evidence")
            evidence_rows.append(evidence)

        if target_state is ArtifactState.QUALIFIED:
            qualified_build = False
            for evidence in evidence_rows:
                if (
                    evidence.kind != "tool.build"
                    or not isinstance(evidence.claims, dict)
                    or not str((evidence.claims or {}).get("buildArtifactDigest") or "").startswith("sha256:")
                    or not isinstance((evidence.claims or {}).get("attestation"), dict)
                ):
                    continue
                try:
                    validate_supply_chain_references((evidence.claims or {}).get("supplyChain"))
                except ValueError:
                    continue
                qualified_build = True
                break
            if not qualified_build:
                raise ValueError(
                    "tool qualification requires trusted build evidence with SPDX/SLSA provenance"
                )
        else:
            promoted = any(
                evidence.kind == "evolution.promotion"
                and isinstance(evidence.claims, dict)
                and str((evidence.claims or {}).get("targetState") or "") == target_state.value
                and str((evidence.claims or {}).get("evaluationEvidenceId") or "").startswith("cevidence_")
                for evidence in evidence_rows
            )
            if not promoted:
                raise ValueError(
                    "learned/certified Tool promotion requires trusted evolution promotion evidence"
                )

        await self._store.set_artifact_state(content_digest, state=target_state.value)

    async def destroy(self, content_digest: str) -> None:
        row = await self.inspect(content_digest)
        if ArtifactState(row.state) is ArtifactState.CORE:
            raise ValueError("core artifact cannot be destroyed by Tool Forge")
        updated = await self._store.set_artifact_state(
            content_digest,
            state=ArtifactState.RETIRED.value,
            retired_at_ms=int(self._clock_ms()),
        )
        if not updated:
            raise KeyError("tool artifact not found")

    @staticmethod
    def _artifact_from_row(row) -> CptrArtifact[Any]:
        return CptrArtifact(
            api_version="cptr.io/v1alpha1",
            kind=ArtifactKind(row.kind),
            metadata=ArtifactMetadata(
                id=row.artifact_id,
                version=row.version,
                owner=ArtifactOwner(row.owner),
                origin=ArtifactOrigin(row.origin),
                created_at=row.created_at,
                user_id=row.user_id,
                parent=row.parent_ref,
                task_origin=row.task_origin,
                source_digest=row.source_digest,
                content_digest=row.content_digest,
            ),
            compatibility=dict(row.compatibility or {}),
            spec=dict(row.spec or {}),
            state=ArtifactState(row.state),
        )
