"""Stable, task-owned storage for transient CPTR execution state."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from cptr.env import TASK_ROOT


_SAFE_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_RUNTIME_CATEGORIES = ("agent", "attachments", "browser", "command-output")


def _runtime_root(root: str | Path | None = None) -> Path:
    return Path(root if root is not None else TASK_ROOT).expanduser().resolve()


def task_runtime_dir(task_id: str, *, root: str | Path | None = None) -> Path:
    """Return the deterministic runtime directory for one task identity."""
    value = str(task_id or "")
    if not _SAFE_TASK_ID.fullmatch(value):
        raise ValueError("task id contains unsafe path characters")
    return _runtime_root(root) / value


def ensure_task_runtime(task_id: str, *, root: str | Path | None = None) -> Path:
    """Create and return the task runtime directory and its owned categories."""
    runtime = task_runtime_dir(task_id, root=root)
    runtime.mkdir(parents=True, exist_ok=True)
    for category in _RUNTIME_CATEGORIES:
        (runtime / category).mkdir(exist_ok=True)
    return runtime


def task_runtime_environment(
    task_id: str,
    *,
    root: str | Path | None = None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an environment with XDG provider state scoped to the task."""
    runtime = ensure_task_runtime(task_id, root=root) / "agent"
    environment = dict(base if base is not None else os.environ)
    environment.update(
        {
            "XDG_CONFIG_HOME": str(runtime / "config"),
            "XDG_DATA_HOME": str(runtime / "data"),
            "XDG_CACHE_HOME": str(runtime / "cache"),
        }
    )
    for path in (runtime / "config", runtime / "data", runtime / "cache"):
        path.mkdir(parents=True, exist_ok=True)
    return environment
