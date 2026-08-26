from __future__ import annotations

from pathlib import Path
from typing import Protocol


class WorkspacePath(Protocol):
    path: str


def is_workspace_available(workspace: WorkspacePath) -> bool:
    """Return whether a persisted workspace still resolves to an accessible directory.

    The caller deliberately receives only a boolean: absolute host paths must never be
    returned in a control-plane availability response or error.
    """
    try:
        return Path(workspace.path).is_dir()
    except OSError:
        return False
