import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
BUILDER = REPO_ROOT / "scripts" / "build_local_release.py"


class LocalReleaseBuilderTests(unittest.TestCase):
    def _run(self, argv: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            argv,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def _make_source_repo(self, root: Path) -> tuple[Path, str]:
        repo = root / "source"
        frontend = repo / "cptr" / "frontend"
        frontend.mkdir(parents=True)
        (repo / "cptr" / "app.py").write_text("APP = True\n", encoding="utf-8")
        (repo / "pyproject.toml").write_text(
            "[project]\nname='fixture'\nversion='1'\n", encoding="utf-8"
        )
        (repo / ".gitignore").write_text("cptr/frontend/build/\n", encoding="utf-8")
        package = {
            "name": "frontend-fixture",
            "version": "1.0.0",
            "private": True,
            "scripts": {
                "build": (
                    "node -e \"const fs=require('fs');"
                    "fs.mkdirSync('build/_app',{recursive:true});"
                    "fs.writeFileSync('build/index.html','<!doctype html><title>CPTR</title>');"
                    "fs.writeFileSync('build/_app/app.js','console.log(1)')\""
                )
            },
        }
        lock = {
            "name": "frontend-fixture",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "requires": True,
            "packages": {"": {"name": "frontend-fixture", "version": "1.0.0"}},
        }
        (frontend / "package.json").write_text(json.dumps(package), encoding="utf-8")
        (frontend / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")

        self._run(["git", "init", "-q"], cwd=repo)
        self._run(["git", "config", "user.name", "CPTR Test"], cwd=repo)
        self._run(["git", "config", "user.email", "cptr-test@example.invalid"], cwd=repo)
        self._run(["git", "add", "."], cwd=repo)
        self._run(["git", "commit", "-qm", "fixture"], cwd=repo)
        revision = self._run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        self.assertFalse((frontend / "build").exists())
        return repo, revision

    def test_builder_generates_verified_frontend_for_exact_git_revision(self):
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            repo, revision = self._make_source_repo(temp)
            release_root = temp / "deploy"

            completed = self._run(
                [
                    sys.executable,
                    str(BUILDER),
                    "--repo",
                    str(repo),
                    "--release-root",
                    str(release_root),
                    "--revision",
                    revision,
                    "--activate",
                ],
                cwd=REPO_ROOT,
            )
            result = json.loads(completed.stdout)
            release = Path(result["release_path"])

            self.assertTrue(release.is_dir())
            self.assertTrue(release.name.startswith(f"{revision}-"))
            self.assertEqual(result["revision"], revision)
            self.assertTrue((release / "cptr" / "frontend" / "build" / "index.html").is_file())
            self.assertTrue((release / "cptr" / "frontend" / "build" / "_app" / "app.js").is_file())
            self.assertFalse((release / "cptr" / "frontend" / "node_modules").exists())
            manifest = json.loads((release / ".cptr-release.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["revision"], revision)
            self.assertEqual(manifest["frontend_sha256"], result["frontend_sha256"])
            current = release_root / "current"
            self.assertTrue(current.is_symlink())
            self.assertEqual(current.resolve(), release.resolve())
            self.assertEqual(result["active_release"], str(release))

    def test_failed_build_never_moves_current_release(self):
        with tempfile.TemporaryDirectory() as temp_value:
            temp = Path(temp_value)
            repo, revision = self._make_source_repo(temp)
            release_root = temp / "deploy"

            first = self._run(
                [
                    sys.executable,
                    str(BUILDER),
                    "--repo",
                    str(repo),
                    "--release-root",
                    str(release_root),
                    "--revision",
                    revision,
                    "--activate",
                ],
                cwd=REPO_ROOT,
            )
            active_before = Path(json.loads(first.stdout)["release_path"]).resolve()

            failed = subprocess.run(
                [
                    sys.executable,
                    str(BUILDER),
                    "--repo",
                    str(repo),
                    "--release-root",
                    str(release_root),
                    "--revision",
                    revision,
                    "--npm-bin",
                    str(temp / "missing-npm"),
                    "--activate",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual((release_root / "current").resolve(), active_before)


if __name__ == "__main__":
    unittest.main()
