"""Shared command-policy classification for CPTR control-plane execution.

This module intentionally treats a shell command string as high risk. It does
not attempt to prove arbitrary user code is offline; instead it rejects shell
composition and wrappers that evade CPTR's explicit package/network policy.
Execution routes must still use named profiles or an argv allowlist when a
stronger operating-system boundary is required.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePath

# Shell control syntax can compose another command, substitute input, redirect
# output, or run a command conditionally. The CPTR direct coding route is not
# a general-purpose shell and therefore fails closed for these forms.
_SHELL_CONTROL_RE = re.compile(r"(?:[;&|<>`]|\$\(|\$\{|\n|\r)")

_SHELL_WRAPPERS = {
    "bash",
    "sh",
    "dash",
    "zsh",
    "fish",
    "ksh",
    "csh",
    "tcsh",
    "cmd",
    "cmd.exe",
    "powershell",
    "pwsh",
    "env",
    "command",
    "xargs",
}
_INTERPRETERS = {
    "python",
    "python3",
    "node",
    "nodejs",
    "ruby",
    "perl",
    "php",
    "lua",
}
_CODE_EVAL_FLAGS = {"-c", "-e", "--eval", "-exec", "--exec"}
_PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "pip", "pip3", "uv", "poetry", "cargo", "gem", "brew", "apt", "apt-get"}
_EXTERNAL_COMMANDS = {
    "curl",
    "wget",
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "nc",
    "ncat",
    "netcat",
    "telnet",
    "ftp",
    "docker",
    "kubectl",
    "terraform",
    "aws",
    "gcloud",
    "az",
    "gh",
}
_DESTRUCTIVE_COMMANDS = {"rm", "rmdir", "shred", "mkfs", "dd", "del", "format"}


def _command_name(token: str) -> str:
    return PurePath(token).name.lower()


def parse_safe_command(command: str) -> tuple[list[str] | None, str | None]:
    """Parse a direct-coding command or return a stable policy violation.

    The caller continues to execute the command through its existing runner,
    but is guaranteed that shell composition and opaque command evaluators did
    not evade policy classification.
    """
    if "\x00" in command:
        return None, "command contains an invalid NUL byte"
    if _SHELL_CONTROL_RE.search(command):
        return None, "shell composition and redirection are not available through direct coding"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return None, "command syntax is invalid"
    if not argv:
        return None, "command must not be blank"

    executable = _command_name(argv[0])
    if executable in _SHELL_WRAPPERS:
        return None, "shell and command-wrapper executables are not available through direct coding"
    if executable in _INTERPRETERS and any(flag in _CODE_EVAL_FLAGS for flag in argv[1:]):
        return None, "interpreter code-evaluation flags are not available through direct coding"
    if executable == "find" and "-exec" in argv:
        return None, "find -exec is not available through direct coding"
    return argv, None


def command_policy_violation(
    command: str,
    *,
    allow_network: bool,
    allow_package_install: bool,
) -> str | None:
    """Return a stable reason when the command exceeds a declared capability.

    The function makes command syntax fail closed and catches known external
    transport/package routes, including paths that previously evaded regular
    expression checks through option placement or interpreters.
    """
    argv, parse_error = parse_safe_command(command)
    if parse_error:
        return parse_error
    assert argv is not None
    executable = _command_name(argv[0])
    args = [arg.lower() for arg in argv[1:]]

    if executable in _DESTRUCTIVE_COMMANDS:
        return "destructive commands are not available through direct coding"
    if executable == "git" and any(arg in {"reset", "clean"} for arg in args):
        return "destructive commands are not available through direct coding"

    is_package_install = (
        (executable in {"npm", "pnpm", "yarn"} and any(arg in {"install", "add", "ci"} for arg in args))
        or (executable in {"pip", "pip3"} and "install" in args)
        or (executable == "uv" and ("sync" in args or ("pip" in args and "install" in args)))
        or (executable == "poetry" and any(arg in {"install", "add", "update"} for arg in args))
        or (executable == "cargo" and "install" in args)
        or (executable == "gem" and "install" in args)
        or (executable in {"brew", "apt", "apt-get"} and any(arg in {"install", "upgrade", "update"} for arg in args))
    )
    if is_package_install and not allow_package_install:
        return "package installation is not permitted by the execution policy"

    is_external = executable in _EXTERNAL_COMMANDS
    if executable == "git" and any(arg in {"push", "fetch", "pull", "clone", "ls-remote", "submodule"} for arg in args):
        is_external = True
    if executable in {"npm", "pnpm", "yarn"} and any(arg in {"publish", "login", "logout", "install", "add", "ci"} for arg in args):
        is_external = True
    if executable in {"pip", "pip3", "uv", "poetry", "cargo", "gem", "brew", "apt", "apt-get"} and is_package_install:
        is_external = True
    if executable == "docker" and any(arg in {"push", "pull", "login", "logout"} for arg in args):
        is_external = True

    if is_external and not allow_network:
        return "external command execution is not permitted by the execution policy"
    return None
