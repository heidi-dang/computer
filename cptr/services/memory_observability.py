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
                "preview": "Persistent user memory" if root.scope == "user" else "Persistent workspace memory",
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
    ) -> None:
        self._session_factory = session_factory
        self._store = store or memory_fabric_store
        self._inventory_builder = inventory_builder or build_memory_inventory
        self._settings_loader = settings_loader or managed_memory.get_memory_settings

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

        inventory = await asyncio.to_thread(
            self._inventory_builder,
            user_id,
            scan_workspaces,
            node_limit=node_limit,
        )
        events = await self._store.list_events(
            user_id,
            workspace=(selected_workspace or {}).get("workspace_path") if workspace_id else None,
            limit=event_limit,
        )
        settings = await self._settings_loader()
        nodes = [dict(node) for node in inventory.get("nodes", [])]
        node_by_id = {str(node.get("id")): node for node in nodes}
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
            if node.get("kind") == "memory":
                node.setdefault("recall_count", 0)
                node.setdefault("last_recalled_at_ms", 0)

        now_ms = int(time.time() * 1000)
        day_ago = now_ms - 86_400_000
        recent = [event for event in events if int(event.get("created_at_ms") or 0) >= day_ago]
        event_metrics = {
            "recalls_24h": sum(1 for event in recent if event.get("event_type") == "recall"),
            "writes_24h": sum(1 for event in recent if event.get("event_type") == "write"),
            "rejected_writes_24h": sum(
                1 for event in recent if event.get("event_type") == "write_rejected"
            ),
            "event_count_visible": len(events),
        }
        metrics = {**inventory.get("metrics", {}), **event_metrics}
        health = {
            "enabled": bool(settings.get("enabled", True)),
            "tool_enabled": bool(settings.get("tool_enabled", True)),
            "background_review_enabled": bool(settings.get("background_review_enabled", False)),
            "review_interval_turns": int(settings.get("review_interval_turns") or 0),
            "canonical_store": "managed_markdown",
            "event_store": "sqlite_append_only",
            "retrieval": "lexical_plus_wiki_graph",
            "realtime": "sse_snapshot_diff",
            "trust_policy": "prompt_injection_filtered",
        }
        fingerprint_payload = {
            "workspace_id": workspace_id,
            "nodes": nodes,
            "edges": inventory.get("edges", []),
            "event_ids": [event.get("event_id") for event in events],
            "health": health,
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str).encode(
                "utf-8"
            )
        ).hexdigest()
        return {
            "version": 1,
            "workspaces": all_workspaces,
            "selected_workspace_id": workspace_id,
            "nodes": nodes,
            "edges": inventory.get("edges", []),
            "events": events,
            "recall_traces": recall_traces[:30],
            "metrics": metrics,
            "health": health,
            "fingerprint": fingerprint,
            "generated_at_ms": now_ms,
        }


memory_observability = MemoryObservabilityService()
