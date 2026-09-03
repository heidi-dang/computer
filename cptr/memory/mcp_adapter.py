"""Extraction-ready MCP adapter over the embedded MemoryService boundary.

This module intentionally contains no memory logic. A host binds an authenticated
principal/workspace when constructing the adapter; model-supplied identity fields are
never accepted as authorization.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from cptr.memory.domain import MemoryQuery, MemoryReplacement
from cptr.memory.service import EmbeddedMemoryService, get_memory_service


class MemoryMcpAdapter:
    def __init__(
        self,
        *,
        user_id: str,
        workspace: str = "",
        service: EmbeddedMemoryService | None = None,
        allow_mutations: bool = False,
        source_forgetter: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        if not str(user_id).strip():
            raise ValueError("authenticated user_id is required")
        self.user_id = str(user_id)
        self.workspace = str(workspace or "")
        self.service = service or get_memory_service()
        self.allow_mutations = bool(allow_mutations)
        self.source_forgetter = source_forgetter

    @staticmethod
    def tool_definitions() -> list[dict[str, Any]]:
        return [
            {
                "name": "memory.search",
                "description": "Search owner-scoped persistent memory using hybrid retrieval.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "maxLength": 12000},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "include_historical": {"type": "boolean"},
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.inspect",
                "description": "Inspect one owner-scoped canonical memory without raw secret/provenance bodies.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"memory_id": {"type": "string", "maxLength": 200}},
                    "required": ["memory_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.timeline",
                "description": "Read bi-temporal memory: valid at at_ms and optionally known by known_at_ms.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "at_ms": {"type": "integer", "minimum": 0},
                        "known_at_ms": {"type": "integer", "minimum": 0},
                    },
                    "required": ["at_ms"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.snapshot",
                "description": "Create an immutable owner-scoped memory snapshot.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"label": {"type": "string", "maxLength": 500}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.verify",
                "description": "Mark a memory as reverified and refresh its verification TTL.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"memory_id": {"type": "string", "maxLength": 200}},
                    "required": ["memory_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.correct",
                "description": "Correct memory by non-destructively superseding an existing fact.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "maxLength": 200},
                        "canonical_text": {"type": "string", "maxLength": 50000},
                    },
                    "required": ["memory_id", "canonical_text"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.forget",
                "description": "Permanently forget one owner-scoped memory and purge its derived indexes.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"memory_id": {"type": "string", "maxLength": 200}},
                    "required": ["memory_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.rebuild",
                "description": "Queue a rebuild of derived memory indexes from canonical truth.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
            {
                "name": "memory.conflicts",
                "description": "List bounded owner-scoped unresolved memory contradictions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                    "additionalProperties": False,
                },
            },
            {
                "name": "memory.health",
                "description": "Read sanitized memory gate, index, maintenance, and intelligence health.",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]

    def _require_mutation(self) -> None:
        if not self.allow_mutations:
            raise PermissionError("memory mutation capability is required")

    @staticmethod
    def _public_record(row: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in row.items()
            if key not in {"source_event_ids", "content_hash", "user_id"}
        }

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments if isinstance(arguments, dict) else {}
        if any(key in args for key in ("user_id", "principal_id", "owner_id", "workspace")):
            raise PermissionError("identity and workspace are bound by the authenticated MCP host")

        if name == "memory.search":
            query = str(args.get("query") or "").strip()
            if not query:
                raise ValueError("query is required")
            results = await self.service.search(
                MemoryQuery(
                    user_id=self.user_id,
                    workspace=self.workspace,
                    query=query,
                    limit=max(1, min(int(args.get("limit") or 12), 100)),
                    include_historical=bool(args.get("include_historical", False)),
                )
            )
            return {
                "results": [
                    {
                        "memory_id": row.memory_id,
                        "scope": row.scope,
                        "kind": row.kind,
                        "text": row.canonical_text,
                        "score": row.score,
                        "reason": row.reason,
                        "confidence": row.confidence,
                        "trust_level": row.trust_level,
                        "verification_stale": row.verification_stale,
                    }
                    for row in results
                ]
            }
        if name == "memory.inspect":
            row = await self.service.inspect(
                str(args.get("memory_id") or ""), user_id=self.user_id, workspace=self.workspace
            )
            return self._public_record(row)
        if name == "memory.timeline":
            at_ms = max(0, int(args.get("at_ms") or 0))
            known_value = args.get("known_at_ms")
            known_at_ms = max(0, int(known_value)) if known_value is not None else None
            rows = await self.service.time_travel(
                self.user_id,
                self.workspace,
                at_ms=at_ms,
                known_at_ms=known_at_ms,
            )
            return {
                "at_ms": at_ms,
                "known_at_ms": known_at_ms,
                "records": [self._public_record(row) for row in rows[:500]],
            }
        if name == "memory.snapshot":
            self._require_mutation()
            ref = await self.service.snapshot(
                self.user_id,
                self.workspace,
                label=str(args.get("label") or "")[:500],
            )
            return {
                "snapshot_id": ref.snapshot_id,
                "memory_version": ref.memory_version,
                "label": ref.label,
                "created_at_ms": ref.created_at_ms,
            }
        if name == "memory.verify":
            self._require_mutation()
            result = await self.service.verify(
                str(args.get("memory_id") or ""),
                user_id=self.user_id,
                workspace=self.workspace,
            )
            return {
                "memory_id": result.memory_id,
                "verified_at_ms": result.verified_at_ms,
                "verification_expires_at_ms": result.verification_expires_at_ms,
                "confidence": result.confidence,
            }
        if name == "memory.correct":
            self._require_mutation()
            memory_id = str(args.get("memory_id") or "")
            text = str(args.get("canonical_text") or "").strip()
            if not memory_id or not text:
                raise ValueError("memory_id and canonical_text are required")
            old = await self.service.inspect(
                memory_id, user_id=self.user_id, workspace=self.workspace
            )
            ref = await self.service.supersede(
                memory_id,
                MemoryReplacement(
                    user_id=self.user_id,
                    workspace=self.workspace,
                    scope=str(old.get("scope") or "workspace"),
                    canonical_text=text,
                    kind=str(old.get("kind") or "semantic"),
                    structured_value={},
                    trust_level="user_directive",
                    confidence=1.0,
                    importance=int(old.get("importance_ppm") or 500_000) / 1_000_000,
                    valid_from_ms=int(time.time() * 1000),
                ),
            )
            return {"memory_id": ref.memory_id, "supersedes": memory_id, "status": ref.status}
        if name == "memory.forget":
            self._require_mutation()
            memory_id = str(args.get("memory_id") or "").strip()
            if not memory_id:
                raise ValueError("memory_id is required")
            await self.service.forget(
                memory_id,
                user_id=self.user_id,
                workspace=self.workspace,
                source_forgetter=self.source_forgetter,
            )
            return {"memory_id": memory_id, "forgotten": True}
        if name == "memory.rebuild":
            self._require_mutation()
            job_id = await self.service.queue_rebuild(self.user_id, self.workspace)
            return {"job_id": job_id, "status": "pending"}
        if name == "memory.conflicts":
            return {
                "conflicts": await self.service.list_conflicts(
                    user_id=self.user_id,
                    workspace=self.workspace,
                    status="open",
                    limit=max(1, min(int(args.get("limit") or 50), 200)),
                )
            }
        if name == "memory.health":
            return await self.service.health(user_id=self.user_id, workspace=self.workspace)
        raise KeyError(f"unknown memory MCP tool: {name}")
