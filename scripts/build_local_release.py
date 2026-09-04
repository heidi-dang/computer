#!/usr/bin/env python3
"""Build a verified immutable local CPTR release from one exact Git commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path


def _run(argv: list[str], *, cwd: Path | None = None, stdout=None) -> None:
    subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=sys.stderr,
        text=False,
    )


def _capture(argv: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
    )
    return completed.stdout.strip()


def _resolve_revision(repo: Path, revision: str) -> str:
    return _capture(["git", "-C", str(repo), "rev-parse", "--verify", f"{revision}^{{commit}}"])


def _assert_repo_root(repo: Path) -> None:
    resolved_root = Path(
        _capture(["git", "-C", str(repo), "rev-parse", "--show-toplevel"])
    ).resolve()
    if resolved_root != repo:
        raise RuntimeError(f"repository path must be the Git root: {repo}")


def _extract_revision(repo: Path, revision: str, destination: Path) -> None:
    with tempfile.TemporaryFile() as archive:
        _run(
            ["git", "-C", str(repo), "archive", "--format=tar", revision],
            stdout=archive,
        )
        archive.seek(0)
        with tarfile.open(fileobj=archive, mode="r:") as bundle:
            bundle.extractall(destination, filter="data")


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(path for path in root.rglob("*") if path.is_file())
    if not files:
        raise RuntimeError(f"release asset tree is empty: {root}")
    for path in files:
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _verify_frontend(release: Path) -> str:
    build = release / "cptr" / "frontend" / "build"
    index = build / "index.html"
    assets = build / "_app"
    if not index.is_file() or index.stat().st_size == 0:
        raise RuntimeError("frontend build is missing a non-empty index.html")
    if not assets.is_dir() or not any(path.is_file() for path in assets.rglob("*")):
        raise RuntimeError("frontend build is missing _app assets")
    if not (release / "cptr" / "app.py").is_file():
        raise RuntimeError("release is missing cptr/app.py")
    if not (release / "pyproject.toml").is_file():
        raise RuntimeError("release is missing pyproject.toml")
    return _tree_digest(build)


def build_release(
    *,
    repo: Path,
    release_root: Path,
    revision: str,
    npm_bin: str = "npm",
) -> dict[str, str]:
    repo = repo.resolve()
    _assert_repo_root(repo)
    exact_revision = _resolve_revision(repo, revision)

    releases = release_root.resolve() / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".staging-{exact_revision[:12]}-", dir=releases))
    keep_stage = True
    try:
        _extract_revision(repo, exact_revision, stage)
        frontend = stage / "cptr" / "frontend"
        if (
            not (frontend / "package.json").is_file()
            or not (frontend / "package-lock.json").is_file()
        ):
            raise RuntimeError("release source is missing locked frontend package metadata")

        _run([npm_bin, "ci"], cwd=frontend, stdout=sys.stderr)
        _run([npm_bin, "run", "build"], cwd=frontend, stdout=sys.stderr)
        shutil.rmtree(frontend / "node_modules", ignore_errors=True)
        shutil.rmtree(frontend / ".svelte-kit", ignore_errors=True)

        frontend_digest = _verify_frontend(stage)
        release_name = f"{exact_revision}-{frontend_digest[:12]}"
        final = releases / release_name
        manifest = {
            "schema": 1,
            "revision": exact_revision,
            "frontend_sha256": frontend_digest,
            "created_at": int(time.time()),
        }
        (stage / ".cptr-release.json").write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

        if final.exists():
            existing_manifest = final / ".cptr-release.json"
            if not existing_manifest.is_file():
                raise RuntimeError(f"release identity collision without manifest: {final}")
            existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
            if (
                existing.get("revision") != exact_revision
                or existing.get("frontend_sha256") != frontend_digest
            ):
                raise RuntimeError(f"release identity collision with different content: {final}")
            shutil.rmtree(stage)
            keep_stage = False
        else:
            os.replace(stage, final)
            keep_stage = False

        _verify_frontend(final)
        return {
            "revision": exact_revision,
            "frontend_sha256": frontend_digest,
            "release_path": str(final),
        }
    finally:
        if keep_stage:
            shutil.rmtree(stage, ignore_errors=True)


def activate_release(*, release_root: Path, release_path: Path) -> str:
    """Atomically point ``current`` at one already-verified release."""
    root = release_root.resolve()
    release = release_path.resolve()
    expected_parent = root / "releases"
    if release.parent != expected_parent or not release.is_dir():
        raise RuntimeError(f"release is outside the managed release directory: {release}")
    _verify_frontend(release)

    current = root / "current"
    if os.path.lexists(current) and not current.is_symlink():
        raise RuntimeError(f"refusing to replace non-symlink current path: {current}")
    previous = str(current.resolve(strict=False)) if current.is_symlink() else ""
    temporary = root / f".current-{os.getpid()}-{time.time_ns()}"
    try:
        os.symlink(release, temporary, target_is_directory=True)
        os.replace(temporary, current)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()
    return previous


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--revision", default="HEAD")
    parser.add_argument("--npm-bin", default=os.environ.get("CPTR_NPM_BIN", "npm"))
    parser.add_argument(
        "--activate",
        action="store_true",
        help="atomically update RELEASE_ROOT/current only after release verification succeeds",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = build_release(
            repo=args.repo,
            release_root=args.release_root,
            revision=args.revision,
            npm_bin=args.npm_bin,
        )
        if args.activate:
            result["previous_release"] = activate_release(
                release_root=args.release_root,
                release_path=Path(result["release_path"]),
            )
            result["active_release"] = result["release_path"]
    except (OSError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"local release build failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
