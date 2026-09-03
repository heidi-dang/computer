"""Owner-scoped realtime projection for CPTR's managed-memory observatory."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from cptr.memory.graph import MemoryGraphStore, memory_graph_store
from cptr.memory.jobs import MemoryJobStore, memory_job_store
from cptr.memory.store import SqlMemoryStore
from cptr.models import Workspace
from cptr.services.memory_fabric import MemoryFabricStore, memory_fabric_store
from cptr.utils import memory as managed_memory
from cptr.utils.db import get_db

InventoryBuilder = Callable[..., dict[str, Any]]


def _node_id(
    scope: str,
    workspace_path: str,
    path: str,
    heading: str = "",
    memory_id: str = "",
) -> str:
    seed = "\x1f".join((scope, workspace_path, path, heading, memory_id))
    return "mem_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def memory_node_id(
    *,
    scope: str,
    workspace_path: str,
    path: str,
    heading: str = "",
    memory_id: str = "",
) -> str:
    """Stable public identifier shared by recall telemetry and the graph projection."""
    return _node_id(scope, workspace_path, path, heading, memory_id)


def _scope_node_id(scope: str, workspace_id: str | None = None) -> str:
    return "scope_user" if scope == "user" else f"scope_workspace_{workspace_id or 'unknown'}"


def _alias(value: str) -> str:
    return " ".join(value.strip().lower().replace("_", " ").replace("-", " ").split())


def _safe_preview(value: str, limit: int = 380) -> str:
    text = " ".join(str(value).split())
    if managed_memory.PROMPT_THREAT_RE.search(text):
        return "[unsafe instruction-like memory hidden]"
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _memory_roots(
    user_id: str, workspaces: list[dict[str, str]]
) -> list[tuple[managed_memory.MemoryRoot, str | None, str, str]]:
    roots: list[tuple[managed_memory.MemoryRoot, str | None, str, str]] = []
    user_root = managed_memory.resolve_memory_roots(user_id)[0]
    roots.append((user_root, None, "User Memory", ""))
    for workspace in workspaces:
        workspace_path = workspace.get("workspace_path") or ""
        if not workspace_path:
            continue
        resolved = managed_memory.resolve_memory_roots(user_id, workspace_path)
        if len(resolved) < 2:
            continue
        roots.append(
            (
                resolved[1],
                workspace.get("workspace_id"),
                workspace.get("workspace_name") or Path(workspace_path).name or "Workspace",
                workspace_path,
            )
        )
    return roots


def build_memory_inventory(
    user_id: str,
    workspaces: list[dict[str, str]],
    *,
    node_limit: int = 400,
) -> dict[str, Any]:
    """Project the canonical Markdown vault into bounded graph nodes and edges."""
    safe_limit = max(25, min(int(node_limit), 2_000))
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    aliases: dict[tuple[str, str, str], str] = {}
    pending_links: list[tuple[str, str, str, str]] = []
    file_count = 0
    total_bytes = 0
    user_count = 0
    workspace_count = 0
    memory_count = 0

    roots = _memory_roots(user_id, workspaces)
    for root, workspace_id, workspace_name, workspace_path in roots:
        scope_key = workspace_path if root.scope == "workspace" else ""
        scope_node = _scope_node_id(root.scope, workspace_id)
        nodes.append(
            {
                "id": scope_node,
                "label": workspace_name,
                "kind": "scope",
                "scope": root.scope,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name if root.scope == "workspace" else None,
                "path": "",
                "heading": "",
                "memory_id": "",
                "preview": "Persistent user memory"
                if root.scope == "user"
                else "Persistent workspace memory",
                "modified_at_ms": 0,
                "size": 0,
                "trust_level": "managed_memory",
                "confidence": 1.0,
                "status": "active",
            }
        )
        if not root.root.is_dir():
            continue
        for path in managed_memory._iter_markdown_files(root.root, include_trash=False):
            if memory_count >= safe_limit:
                break
            try:
                stat = path.stat()
                text = path.read_text(errors="replace")
            except OSError:
                continue
            file_count += 1
            total_bytes += int(stat.st_size)
            rel = str(managed_memory._relative_to_root(path, root.root))
            sections = managed_memory._split_markdown_sections(text, path.stem)
            rows: list[dict[str, str]] = []
            if path == root.baseline_path and len(sections) <= 1:
                entries = managed_memory.read_memory_entries(path)
                if entries:
                    rows = [
                        {
                            "heading": entry,
                            "memory_id": "",
                            "content": entry,
                        }
                        for entry in entries
                    ]
            if not rows:
                rows = [
                    {
                        "heading": str(section.get("heading") or path.stem),
                        "memory_id": str(section.get("memory_id") or ""),
                        "content": str(section.get("content") or ""),
                    }
                    for section in sections
                    if str(section.get("content") or "").strip()
                ]
            for row in rows:
                if memory_count >= safe_limit:
                    break
                heading = row["heading"]
                memory_id = row["memory_id"]
                content = row["content"]
                node_id = _node_id(root.scope, scope_key, rel, heading, memory_id)
                links = managed_memory._extract_wiki_links(content)
                node = {
                    "id": node_id,
                    "label": _safe_preview(heading, 72) or path.stem,
                    "kind": "memory",
                    "scope": root.scope,
                    "workspace_id": workspace_id,
                    "workspace_name": workspace_name if root.scope == "workspace" else None,
                    "path": rel,
                    "heading": heading,
                    "memory_id": memory_id,
                    "preview": _safe_preview(content),
                    "links": links[:12],
                    "modified_at_ms": int(stat.st_mtime * 1000),
                    "size": len(content.encode("utf-8", errors="replace")),
                    "trust_level": "managed_memory",
                    "confidence": 1.0,
                    "status": "active",
                }
                nodes.append(node)
                memory_count += 1
                if root.scope == "user":
                    user_count += 1
                else:
                    workspace_count += 1
                edges.append(
                    {
                        "id": f"edge_scope_{node_id}",
                        "source": node_id,
                        "target": scope_node,
                        "kind": "belongs_to",
                    }
                )
                alias_scope = workspace_id or "user"
                for candidate in {
                    heading,
                    memory_id,
                    rel,
                    rel.removesuffix(".md"),
                    path.stem,
                }:
                    normalized = _alias(candidate)
                    if normalized:
                        aliases.setdefault((root.scope, alias_scope, normalized), node_id)
                for link in links[:12]:
                    pending_links.append((node_id, root.scope, alias_scope, link))
        if memory_count >= safe_limit:
            break

    for source, scope, alias_scope, link in pending_links:
        normalized = _alias(link)
        target = aliases.get((scope, alias_scope, normalized))
        if target is None and scope == "workspace":
            target = aliases.get(("user", "user", normalized))
        if target and target != source:
            edge_seed = f"{source}\x1f{target}\x1f{normalized}"
            edges.append(
                {
                    "id": "edge_rel_" + hashlib.sha256(edge_seed.encode("utf-8")).hexdigest()[:20],
                    "source": source,
                    "target": target,
                    "kind": "related",
                    "label": link,
                }
            )

    return {
        "nodes": nodes,
        "edges": edges,
        "metrics": {
            "memory_nodes": memory_count,
            "user_memory_nodes": user_count,
            "workspace_memory_nodes": workspace_count,
            "scope_nodes": len(roots),
            "edge_count": len(edges),
            "file_count": file_count,
            "total_bytes": total_bytes,
            "truncated": memory_count >= safe_limit,
        },
    }


class MemoryObservabilityService:
    def __init__(
        self,
        *,
        session_factory=None,
        store: MemoryFabricStore | None = None,
        inventory_builder: InventoryBuilder | None = None,
        settings_loader=None,
        core_store: SqlMemoryStore | None = None,
        job_store: MemoryJobStore | None = None,
        graph_store: MemoryGraphStore | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._store = store or memory_fabric_store
        self._inventory_builder = inventory_builder or build_memory_inventory
        self._settings_loader = settings_loader or managed_memory.get_memory_settings
        self._core_store = core_store or SqlMemoryStore(session_factory=session_factory)
        self._job_store = job_store or (
            memory_job_store
            if session_factory is None
            else MemoryJobStore(session_factory=session_factory)
        )
        self._graph_store = graph_store or (
            memory_graph_store
            if session_factory is None
            else MemoryGraphStore(session_factory=session_factory)
        )

    @asynccontextmanager
    async def _session(self):
        if self._session_factory is not None:
            async with self._session_factory() as db:
                yield db
            return
        async with await get_db() as db:
            yield db

    async def _workspaces(self, user_id: str) -> list[dict[str, str]]:
        async with self._session() as db:
            rows = list(
                (
                    await db.scalars(
                        select(Workspace)
                        .where(Workspace.user_id == user_id)
                        .order_by(Workspace.name.asc(), Workspace.id.asc())
                    )
                ).all()
            )
        return [
            {
                "workspace_id": row.id,
                "workspace_name": row.name,
                "workspace_path": row.path,
            }
            for row in rows
        ]

    async def snapshot(
        self,
        *,
        user_id: str,
        workspace_id: str | None = None,
        node_limit: int = 400,
        event_limit: int = 120,
    ) -> dict[str, Any]:
        all_workspaces = await self._workspaces(user_id)
        selected_workspace = None
        scan_workspaces = all_workspaces
        if workspace_id:
            selected_workspace = next(
                (row for row in all_workspaces if row["workspace_id"] == workspace_id), None
            )
            if selected_workspace is None:
                raise KeyError("memory workspace not found")
            scan_workspaces = [selected_workspace]

        selected_workspace_path = (
            (selected_workspace or {}).get("workspace_path") if workspace_id else None
        )
        inventory = await asyncio.to_thread(
            self._inventory_builder,
            user_id,
            scan_workspaces,
            node_limit=node_limit,
        )
        events = await self._store.list_events(
            user_id,
            workspace=selected_workspace_path,
            limit=event_limit,
        )
        settings = await self._settings_loader()
        core_state = await self._core_store.observability_snapshot(
            user_id=user_id,
            workspace=selected_workspace_path,
            limit=node_limit,
        )
        graph_state = await self._graph_store.snapshot(
            user_id=user_id,
            workspace=selected_workspace_path,
            limit=node_limit,
        )
        job_counts = await self._job_store.counts(
            user_id=user_id,
            workspace=selected_workspace_path,
        )

        now_ms = int(time.time() * 1000)
        workspace_by_path = {str(row.get("workspace_path") or ""): row for row in all_workspaces}
        nodes = [dict(node) for node in inventory.get("nodes", [])]
        edges = [dict(edge) for edge in inventory.get("edges", [])]
        node_by_id = {str(node.get("id")): node for node in nodes}

        def ensure_scope_node(scope: str, workspace_path: str) -> str:
            workspace = workspace_by_path.get(workspace_path) if workspace_path else None
            scope_id = _scope_node_id(
                scope,
                str((workspace or {}).get("workspace_id") or "") or None,
            )
            if scope_id not in node_by_id:
                label = (
                    str((workspace or {}).get("workspace_name") or "")
                    or (Path(workspace_path).name if workspace_path else "User Memory")
                    or "Workspace"
                )
                node = {
                    "id": scope_id,
                    "label": label,
                    "kind": "scope",
                    "scope": scope,
                    "workspace_id": (workspace or {}).get("workspace_id"),
                    "workspace_name": (workspace or {}).get("workspace_name"),
                    "path": "",
                    "heading": "",
                    "memory_id": "",
                    "preview": "Persistent user memory"
                    if scope == "user"
                    else "Persistent workspace memory",
                    "modified_at_ms": 0,
                    "size": 0,
                    "trust_level": "managed_memory",
                    "confidence": 1.0,
                    "status": "active",
                    "source_layer": "scope",
                }
                nodes.append(node)
                node_by_id[scope_id] = node
            return scope_id

        canonical_records = list(core_state.get("records") or [])
        for record in canonical_records:
            memory_id = str(record.get("memory_id") or "")
            if not memory_id or memory_id in node_by_id:
                continue
            workspace_path = str(record.get("workspace") or "")
            scope = str(record.get("scope") or "")
            if scope not in {"user", "workspace"}:
                scope = "workspace" if workspace_path else "user"
            workspace = workspace_by_path.get(workspace_path) if workspace_path else None
            confidence = max(0.0, min(1.0, int(record.get("confidence_ppm") or 0) / 1_000_000))
            importance = max(0.0, min(1.0, int(record.get("importance_ppm") or 0) / 1_000_000))
            verification_expires_at_ms = record.get("verification_expires_at_ms")
            node = {
                "id": memory_id,
                "label": _safe_preview(str(record.get("canonical_text") or ""), 72)
                or str(record.get("kind") or "Memory").replace("_", " ").title(),
                "kind": "memory",
                "scope": scope,
                "workspace_id": (workspace or {}).get("workspace_id"),
                "workspace_name": (workspace or {}).get("workspace_name"),
                "path": "canonical",
                "heading": str(record.get("kind") or "semantic").replace("_", " ").title(),
                "memory_id": memory_id,
                "preview": _safe_preview(str(record.get("canonical_text") or "")),
                "modified_at_ms": int(record.get("updated_at_ms") or 0),
                "size": len(str(record.get("canonical_text") or "").encode("utf-8")),
                "trust_level": str(record.get("trust_level") or "agent_observation"),
                "confidence": confidence,
                "importance": importance,
                "status": str(record.get("status") or "active"),
                "source_layer": "canonical",
                "valid_from_ms": record.get("valid_from_ms"),
                "valid_until_ms": record.get("valid_until_ms"),
                "superseded_by_id": record.get("superseded_by_id"),
                "parent_memory_id": record.get("parent_memory_id"),
                "branch_id": record.get("branch_id"),
                "verified_at_ms": record.get("verified_at_ms"),
                "verification_expires_at_ms": verification_expires_at_ms,
                "verification_stale": verification_expires_at_ms is not None
                and int(verification_expires_at_ms) <= now_ms,
                "recall_count": int(record.get("access_count") or 0),
                "last_recalled_at_ms": int(record.get("last_accessed_at_ms") or 0),
            }
            nodes.append(node)
            node_by_id[memory_id] = node
            scope_id = ensure_scope_node(scope, workspace_path)
            edges.append(
                {
                    "id": f"edge_scope_{memory_id}",
                    "source": memory_id,
                    "target": scope_id,
                    "kind": "belongs_to",
                }
            )

        for record in canonical_records:
            memory_id = str(record.get("memory_id") or "")
            superseded_by = str(record.get("superseded_by_id") or "")
            parent_memory = str(record.get("parent_memory_id") or "")
            if memory_id in node_by_id and superseded_by in node_by_id:
                edges.append(
                    {
                        "id": f"edge_superseded_{memory_id}_{superseded_by}",
                        "source": memory_id,
                        "target": superseded_by,
                        "kind": "related",
                        "label": "superseded by",
                    }
                )
            if memory_id in node_by_id and parent_memory in node_by_id:
                edges.append(
                    {
                        "id": f"edge_parent_{memory_id}_{parent_memory}",
                        "source": memory_id,
                        "target": parent_memory,
                        "kind": "related",
                        "label": "derived from",
                    }
                )

        for entity in graph_state.get("entities") or []:
            entity_id = str(entity.get("entity_id") or "")
            if not entity_id or entity_id in node_by_id:
                continue
            workspace_path = str(entity.get("workspace") or "")
            scope = "workspace" if workspace_path else "user"
            workspace = workspace_by_path.get(workspace_path) if workspace_path else None
            node = {
                "id": entity_id,
                "label": _safe_preview(str(entity.get("canonical_name") or "Entity"), 72),
                "kind": "entity",
                "scope": scope,
                "workspace_id": (workspace or {}).get("workspace_id"),
                "workspace_name": (workspace or {}).get("workspace_name"),
                "path": "entity",
                "heading": str(entity.get("entity_type") or "concept").replace("_", " ").title(),
                "memory_id": "",
                "preview": ", ".join(str(alias) for alias in entity.get("aliases") or [])[:380],
                "modified_at_ms": int(entity.get("updated_at_ms") or 0),
                "size": 0,
                "trust_level": "derived_entity",
                "confidence": 1.0,
                "status": str(entity.get("status") or "active"),
                "source_layer": "entity",
                "entity_type": str(entity.get("entity_type") or "concept"),
                "valid_from_ms": entity.get("valid_from_ms"),
                "valid_until_ms": entity.get("valid_until_ms"),
            }
            nodes.append(node)
            node_by_id[entity_id] = node
            scope_id = ensure_scope_node(scope, workspace_path)
            edges.append(
                {
                    "id": f"edge_scope_{entity_id}",
                    "source": entity_id,
                    "target": scope_id,
                    "kind": "belongs_to",
                }
            )

        for relationship in graph_state.get("relationships") or []:
            source = str(relationship.get("source_entity_id") or "")
            target = str(relationship.get("target_entity_id") or "")
            if source in node_by_id and target in node_by_id:
                edges.append(
                    {
                        "id": str(
                            relationship.get("relationship_id") or f"edge_entity_{source}_{target}"
                        ),
                        "source": source,
                        "target": target,
                        "kind": "related",
                        "label": str(relationship.get("relation") or "related to"),
                    }
                )

        recall_traces: list[dict[str, Any]] = []
        for event in events:
            if event.get("event_type") != "recall":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            items = payload.get("items") if isinstance(payload, dict) else []
            if not isinstance(items, list):
                items = []
            shaped_items: list[dict[str, Any]] = []
            for raw in items[:50]:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                node_id = str(item.get("node_id") or "")
                node = node_by_id.get(node_id)
                if node is not None:
                    node["recall_count"] = int(node.get("recall_count") or 0) + 1
                    node["last_recalled_at_ms"] = max(
                        int(node.get("last_recalled_at_ms") or 0), int(event["created_at_ms"])
                    )
                shaped_items.append(item)
            recall_traces.append(
                {
                    "event_id": event["event_id"],
                    "created_at_ms": event["created_at_ms"],
                    "workspace": event.get("workspace"),
                    "context_chars": int(payload.get("context_chars") or 0),
                    "items": shaped_items,
                }
            )
        for node in nodes:
            if node.get("kind") in {"memory", "entity"}:
                node.setdefault("recall_count", 0)
                node.setdefault("last_recalled_at_ms", 0)

        day_ago = now_ms - 86_400_000
        recent = [event for event in events if int(event.get("created_at_ms") or 0) >= day_ago]
        managed_count = int(inventory.get("metrics", {}).get("memory_nodes") or 0)
        canonical_count = len(canonical_records)
        entity_count = len(graph_state.get("entities") or [])
        namespace_versions = [
            int(row.get("version") or 0) for row in core_state.get("namespaces") or []
        ]
        event_metrics = {
            "recalls_24h": sum(1 for event in recent if event.get("event_type") == "recall"),
            "writes_24h": sum(1 for event in recent if event.get("event_type") == "write"),
            "rejected_writes_24h": sum(
                1 for event in recent if event.get("event_type") == "write_rejected"
            ),
            "event_count_visible": len(events),
        }
        metrics = {
            **inventory.get("metrics", {}),
            **event_metrics,
            "managed_memory_nodes": managed_count,
            "canonical_memory_nodes": canonical_count,
            "entity_nodes": entity_count,
            "memory_nodes": managed_count + canonical_count,
            "edge_count": len(edges),
            "superseded_memory_nodes": sum(
                1 for row in canonical_records if row.get("status") == "superseded"
            ),
            "stale_verification_nodes": sum(
                1
                for row in canonical_records
                if row.get("verification_expires_at_ms") is not None
                and int(row["verification_expires_at_ms"]) <= now_ms
            ),
            "snapshot_count": len(core_state.get("snapshots") or []),
            "branch_count": len(core_state.get("branches") or []),
            "checkpoint_count": len(core_state.get("checkpoints") or []),
            "memory_version": max(namespace_versions, default=0),
            "pending_memory_jobs": int(job_counts.get("pending") or 0),
            "running_memory_jobs": int(job_counts.get("running") or 0),
            "failed_memory_jobs": int(job_counts.get("failed") or 0),
        }
        health = {
            "enabled": bool(settings.get("enabled", True)),
            "tool_enabled": bool(settings.get("tool_enabled", True)),
            "background_review_enabled": bool(settings.get("background_review_enabled", False)),
            "review_interval_turns": int(settings.get("review_interval_turns") or 0),
            "canonical_store": "managed_markdown_plus_versioned_memory_records",
            "event_store": "sqlite_append_only",
            "retrieval": "hybrid_lexical_similarity_vector_port_plus_graph",
            "realtime": "sse_snapshot_diff",
            "trust_policy": "prompt_injection_filtered_and_secret_redacted",
            "required_for_execution": bool(settings.get("required_for_execution", True)),
            "context_char_limit": int(settings.get("context_char_limit") or 9000),
            "verification_ttl_seconds": int(settings.get("verification_ttl_seconds") or 0),
            "maintenance_enabled": bool(settings.get("maintenance_enabled", True)),
            "maintenance_queue": job_counts,
        }
        lifecycle = {
            "namespaces": list(core_state.get("namespaces") or []),
            "checkpoints": list(core_state.get("checkpoints") or []),
            "snapshots": list(core_state.get("snapshots") or []),
            "branches": list(core_state.get("branches") or []),
        }
        fingerprint_payload = {
            "workspace_id": workspace_id,
            "nodes": nodes,
            "edges": edges,
            "event_ids": [event.get("event_id") for event in events],
            "health": health,
            "lifecycle": lifecycle,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode("utf-8")
        ).hexdigest()
        return {
            "version": 2,
            "workspaces": all_workspaces,
            "selected_workspace_id": workspace_id,
            "nodes": nodes,
            "edges": edges,
            "events": events,
            "recall_traces": recall_traces[:30],
            "metrics": metrics,
            "health": health,
            "lifecycle": lifecycle,
            "fingerprint": fingerprint,
            "generated_at_ms": now_ms,
        }


memory_observability = MemoryObservabilityService()
