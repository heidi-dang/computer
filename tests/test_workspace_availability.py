from pathlib import Path
from types import SimpleNamespace

from cptr.services.workspace_availability import is_workspace_available


def test_workspace_availability_accepts_existing_directory(tmp_path: Path) -> None:
    assert is_workspace_available(SimpleNamespace(path=str(tmp_path)))


def test_workspace_availability_rejects_missing_path_without_returning_it(tmp_path: Path) -> None:
    stale_path = tmp_path / "removed-workspace"
    assert not is_workspace_available(SimpleNamespace(path=str(stale_path)))
