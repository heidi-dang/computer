import asyncio
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cptr.flowdeck.git import (
    GitInspectionError,
    GitInspectionRequest,
    MAX_OUTPUT_BYTES,
    _git,
    inspect_git,
)


class CanonicalGitSecurityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "tracked.txt").write_text("initial\n")
        subprocess.run(["git", "-C", str(self.root), "add", "tracked.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "initial",
            ],
            check=True,
        )
        self.marker = Path(self.temp.name) / "executed"
        self.authorized = str(self.root)

    async def asyncTearDown(self):
        self.temp.cleanup()

    def _snapshot(self):
        return subprocess.check_output(
            ["git", "-C", str(self.root), "status", "--porcelain=v2", "--branch"]
        ), (self.root / ".git" / "HEAD").read_bytes(), (
            self.root / ".git" / "index"
        ).read_bytes()

    def _write_executable(self, name, body):
        path = Path(self.temp.name) / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return path

    async def test_hostile_local_config_hooks_fsmonitor_and_tools_have_no_effect(self):
        marker = str(self.marker)
        (self.root / ".git" / "config").write_text(
            "[core]\n"
            f"fsmonitor = {self._write_executable('fsmonitor', f'#!/bin/sh\\ntouch {marker}\\n')}\n"
            f"pager = {self._write_executable('pager', f'#!/bin/sh\\ntouch {marker}\\n')}\n"
            f"editor = {self._write_executable('editor', f'#!/bin/sh\\ntouch {marker}\\n')}\n"
            f"sequenceEditor = {self._write_executable('seq-editor', f'#!/bin/sh\\ntouch {marker}\\n')}\n"
            f"sshCommand = {self._write_executable('ssh', f'#!/bin/sh\\ntouch {marker}\\n')}\n"
            "[diff]\n"
            f"external = {self._write_executable('external', f'#!/bin/sh\\ntouch {marker}\\n')}\n"
            f"command = {self._write_executable('textconv', f'#!/bin/sh\\ntouch {marker}\\n')}\n"
            "[alias]\n"
            f"status = !touch {marker}\n"
            "[credential]\n"
            f"helper = !touch {marker}\n"
        )
        hooks = self.root / ".git" / "hooks"
        hooks.mkdir(exist_ok=True)
        hook = hooks / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

        for operation in ("status", "log", "diff"):
            await inspect_git(
                GitInspectionRequest(self.authorized, operation),
                authorized_workspace=self.authorized,
            )
        self.assertFalse(self.marker.exists())

    async def test_hostile_global_and_system_config_are_ignored(self):
        marker = str(self.marker)
        config = Path(self.temp.name) / "hostile-config"
        config.write_text(f"[core]\npager = !touch {marker}\n")
        with patch.dict(
            os.environ,
            {
                "HOME": self.temp.name,
                "GIT_CONFIG_GLOBAL": str(config),
                "GIT_CONFIG_SYSTEM": str(config),
            },
        ):
            await inspect_git(
                GitInspectionRequest(self.authorized, "status"),
                authorized_workspace=self.authorized,
            )
        self.assertFalse(self.marker.exists())

    async def test_symlink_alias_is_canonicalized_but_other_repo_is_rejected(self):
        alias = Path(self.temp.name) / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        result = await inspect_git(
            GitInspectionRequest(str(alias), "status"),
            authorized_workspace=self.authorized,
        )
        self.assertEqual(Path(result["workspace"]), self.root.resolve())

        other = Path(self.temp.name) / "other"
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        other_alias = Path(self.temp.name) / "other-alias"
        other_alias.symlink_to(other, target_is_directory=True)
        with self.assertRaises(GitInspectionError):
            await inspect_git(
                GitInspectionRequest(str(other_alias), "status"),
                authorized_workspace=self.authorized,
            )

    async def test_authorization_and_option_injection_fail_closed(self):
        sibling = Path(self.temp.name) / "sibling"
        sibling.mkdir()
        for workspace in (sibling, self.root.parent, self.root / "tracked.txt"):
            with self.assertRaises(GitInspectionError):
                await inspect_git(
                    GitInspectionRequest(str(workspace), "status"),
                    authorized_workspace=self.authorized,
                )
        for operation in ("--help", "status --help", "commit", "config", "push"):
            with self.assertRaises(GitInspectionError):
                await inspect_git(
                    GitInspectionRequest(self.authorized, operation),
                    authorized_workspace=self.authorized,
                )
        with self.assertRaises(GitInspectionError):
            await inspect_git(
                GitInspectionRequest(self.authorized, "log", limit=51),
                authorized_workspace=self.authorized,
            )

    async def test_allowed_operations_do_not_mutate_repository_or_leave_locks(self):
        before = self._snapshot()
        for operation in ("status", "log", "diff"):
            await inspect_git(
                GitInspectionRequest(self.authorized, operation),
                authorized_workspace=self.authorized,
            )
        self.assertEqual(before, self._snapshot())
        self.assertEqual(list((self.root / ".git").glob("*.lock")), [])
        self.assertEqual(
            (self.root / ".git" / "config").read_bytes(),
            (self.root / ".git" / "config").read_bytes(),
        )

    async def _fake_git_failure(self, stream, output, expected):
        fake_dir = Path(self.temp.name) / "bin"
        fake_dir.mkdir()
        pid_file = Path(self.temp.name) / "pid"
        script = f"#!/bin/sh\nprintf '%s' $$ > '{pid_file}'\n"
        if stream == "stdout":
            script += f"printf '%*s' {len(output)} x\n"
        else:
            script += f"printf '%*s' {len(output)} x >&2\n"
        script += "while :; do sleep 1; done\n"
        self._write_executable("git", script).rename(fake_dir / "git")
        env = {"PATH": str(fake_dir), "HOME": str(self.root)}
        with patch.dict(os.environ, env, clear=False):
            with self.assertRaises(GitInspectionError):
                await _git(self.root, "status", output_limit=128)
        self.assertTrue(pid_file.exists())
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_stdout_overflow_terminates_and_reaps_process(self):
        await self._fake_git_failure("stdout", "x" * 1024, "stdout")

    async def test_stderr_overflow_terminates_and_reaps_process(self):
        await self._fake_git_failure("stderr", "x" * 1024, "stderr")

    async def test_timeout_terminates_and_reaps_process(self):
        fake_dir = Path(self.temp.name) / "timeout-bin"
        fake_dir.mkdir()
        pid_file = Path(self.temp.name) / "timeout-pid"
        self._write_executable(
            "git-timeout",
            f"#!/bin/sh\nprintf '%s' $$ > '{pid_file}'\nwhile :; do sleep 1; done\n",
        ).rename(fake_dir / "git")
        with patch.dict(os.environ, {"PATH": str(fake_dir)}, clear=False):
            with patch("cptr.flowdeck.git.INSPECTION_TIMEOUT_SECONDS", 0.05):
                with self.assertRaises(GitInspectionError):
                    await _git(self.root, "status")
        pid = int(pid_file.read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()