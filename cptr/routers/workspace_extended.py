"""Extended Workspace API endpoints.

GET  /api/workspace/files/tree              – recursive file-tree JSON
POST /api/workspace/files/bulk-read         – read multiple files in one request
POST /api/workspace/files/bulk-delete       – delete multiple files/dirs in one call
GET  /api/workspace/files/recent            – recently modified files
POST /api/workspace/files/rename            – rename a file or directory
GET  /api/workspace/stats                   – workspace stats (disk, file count, languages)
POST /api/workspace/zip                     – create and download a ZIP of selected files
"""

from __future__ import annotations

import asyncio
import io
import os
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from cptr.utils.runtime import Runtime, FileError

router = APIRouter(prefix="/api/workspace", tags=["workspace-extended"])


# ── Request models ────────────────────────────────────────────────────────────


class BulkReadRequest(BaseModel):
    paths: list[str]


class BulkDeleteRequest(BaseModel):
    paths: list[str]


class RenameRequest(BaseModel):
    path: str
    new_name: str  # just the filename, no directory component


class ZipRequest(BaseModel):
    paths: list[str]
    name: Optional[str] = "workspace.zip"


# ── File tree ────────────────────────────────────────────────────────────────


@router.get("/files/tree")
async def file_tree(
    request: Request,
    path: str = Query(..., description="Absolute root path to tree from"),
    max_depth: int = Query(8, ge=1, le=20),
    show_hidden: bool = Query(False),
):
    """Return a full recursive file-tree JSON for a directory."""
    try:
        result = await Runtime.list_tree(request, path, recursive=True)
        return {
            "path": path,
            "tree": result.get("entries", []),
            "total": result.get("total", 0),
        }
    except FileError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Bulk read ────────────────────────────────────────────────────────────────


@router.post("/files/bulk-read")
async def bulk_read_files(request: Request, body: BulkReadRequest):
    """Read multiple files in one request — returns array of {path, content, error}."""
    if not body.paths:
        return {"files": []}
    if len(body.paths) > 100:
        raise HTTPException(400, "Maximum 100 files per bulk-read")

    async def _read_one(path: str) -> dict:
        try:
            result = await Runtime.read_file(request, path)
            return {"path": path, "content": result.get("content"), "binary": result.get("binary", False), "error": None}
        except FileError as exc:
            return {"path": path, "content": None, "error": str(exc)}
        except Exception as exc:
            return {"path": path, "content": None, "error": str(exc)}

    results = await asyncio.gather(*[_read_one(p) for p in body.paths])
    return {"files": list(results)}


# ── Bulk delete ──────────────────────────────────────────────────────────────


@router.post("/files/bulk-delete")
async def bulk_delete_files(request: Request, body: BulkDeleteRequest):
    """Delete multiple files/dirs in one call."""
    if not body.paths:
        return {"deleted": [], "errors": []}

    deleted = []
    errors = []
    for path in body.paths:
        try:
            await Runtime.delete_item(request, path)
            deleted.append(path)
        except FileError as exc:
            errors.append({"path": path, "error": str(exc)})
        except Exception as exc:
            errors.append({"path": path, "error": str(exc)})
    return {"deleted": deleted, "errors": errors}


# ── Recent files ────────────────────────────────────────────────────────────


@router.get("/files/recent")
async def recent_files(
    request: Request,
    path: str = Query(..., description="Root path to scan"),
    limit: int = Query(20, ge=1, le=100),
    skip_hidden: bool = Query(True),
):
    """List recently modified files in a workspace (mtime-sorted, newest first)."""

    def _scan() -> list[dict]:
        root = Path(path)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        entries: list[dict] = []
        for item in root.rglob("*"):
            if not item.is_file():
                continue
            if skip_hidden and any(part.startswith(".") for part in item.parts):
                continue
            try:
                stat = item.stat()
                entries.append({
                    "path": str(item),
                    "name": item.name,
                    "size": stat.st_size,
                    "modified_at": int(stat.st_mtime * 1000),
                })
            except OSError:
                continue
        entries.sort(key=lambda e: e["modified_at"], reverse=True)
        return entries[:limit]

    try:
        files = await asyncio.to_thread(_scan)
        return {"path": path, "files": files}
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Rename ───────────────────────────────────────────────────────────────────


@router.post("/files/rename")
async def rename_file(request: Request, body: RenameRequest):
    """Rename a file or directory (same-directory move)."""
    src = Path(body.path)
    if not src.parent or not body.new_name.strip():
        raise HTTPException(400, "Invalid path or new_name")
    # Prevent path traversal in new_name
    bad_chars = "/\\"
    if any(c in body.new_name for c in bad_chars) or body.new_name in (".", ".."):
        raise HTTPException(400, "new_name must be a plain filename, no path separators")
    destination = str(src.parent / body.new_name)
    try:
        return await Runtime.move_item(request, body.path, destination)
    except FileError as exc:
        raise HTTPException(exc.status_code, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ── Stats ────────────────────────────────────────────────────────────────────


@router.get("/stats")
async def workspace_stats(
    request: Request,
    path: str = Query(..., description="Workspace root path"),
):
    """Return workspace stats: disk usage, file count, language breakdown."""
    _LANGUAGE_MAP = {
        ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript", ".jsx": "JavaScript", ".rs": "Rust",
        ".go": "Go", ".java": "Java", ".cpp": "C++", ".c": "C",
        ".cs": "C#", ".rb": "Ruby", ".php": "PHP", ".swift": "Swift",
        ".kt": "Kotlin", ".md": "Markdown", ".html": "HTML",
        ".css": "CSS", ".scss": "CSS", ".json": "JSON", ".yaml": "YAML",
        ".yml": "YAML", ".toml": "TOML", ".sh": "Shell", ".sql": "SQL",
        ".vue": "Vue", ".svelte": "Svelte",
    }

    def _compute() -> dict:
        root = Path(path)
        if not root.exists():
            raise FileNotFoundError(f"Path not found: {path}")
        total_files = 0
        total_dirs = 0
        total_bytes = 0
        language_counts: dict[str, int] = {}
        language_bytes: dict[str, int] = {}
        for item in root.rglob("*"):
            if any(part.startswith(".") for part in item.relative_to(root).parts):
                continue
            if item.is_dir():
                total_dirs += 1
            elif item.is_file():
                total_files += 1
                try:
                    size = item.stat().st_size
                    total_bytes += size
                    lang = _LANGUAGE_MAP.get(item.suffix.lower())
                    if lang:
                        language_counts[lang] = language_counts.get(lang, 0) + 1
                        language_bytes[lang] = language_bytes.get(lang, 0) + size
                except OSError:
                    pass
        langs_sorted = sorted(language_counts.items(), key=lambda x: x[1], reverse=True)
        return {
            "path": path,
            "total_files": total_files,
            "total_dirs": total_dirs,
            "total_bytes": total_bytes,
            "total_human": _human_size(total_bytes),
            "languages": [
                {"language": lang, "files": count, "bytes": language_bytes.get(lang, 0)}
                for lang, count in langs_sorted[:20]
            ],
        }

    try:
        return await asyncio.to_thread(_compute)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size //= 1024
    return f"{size:.1f} TB"


# ── ZIP download ─────────────────────────────────────────────────────────────


@router.post("/zip")
async def create_zip(request: Request, body: ZipRequest):
    """Create and stream a ZIP of selected files or directories."""
    if not body.paths:
        raise HTTPException(400, "No paths provided")

    def _build_zip() -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for raw_path in body.paths:
                p = Path(raw_path)
                if not p.exists():
                    continue
                if p.is_file():
                    zf.write(p, p.name)
                elif p.is_dir():
                    for child in p.rglob("*"):
                        if child.is_file():
                            zf.write(child, str(child.relative_to(p.parent)))
        buf.seek(0)
        return buf.read()

    try:
        data = await asyncio.to_thread(_build_zip)
    except Exception as exc:
        raise HTTPException(500, f"Failed to create ZIP: {exc}")

    filename = body.name or "workspace.zip"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
