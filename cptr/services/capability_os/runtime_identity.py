"""Measured runtime identities for the standalone broker; no generated code is run."""
from __future__ import annotations
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from cptr.services.capability_os.sandbox_daemon import SandboxRuntimeUnavailable

MAX_ROOTFS_BYTES = 512 * 1024 * 1024
MAX_ROOTFS_ENTRIES = 50000


def _metadata(st, expected_uid: int) -> None:
    if st.st_uid != expected_uid or stat.S_IMODE(st.st_mode) & 0o6022:
        raise SandboxRuntimeUnavailable("qualified runtime ownership or permissions mismatch")


def _parents(path: Path, expected_uid: int) -> None:
    if not path.is_absolute():
        raise SandboxRuntimeUnavailable("qualified runtime path must be absolute")
    for parent in path.parents:
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid not in {0, expected_uid}:
            raise SandboxRuntimeUnavailable("qualified runtime has an untrusted parent")
        if st.st_mode & 0o022:
            # A root-owned sticky temporary directory cannot replace an owned child.
            if not (st.st_uid == 0 and st.st_mode & stat.S_ISVTX):
                raise SandboxRuntimeUnavailable("qualified runtime parent is writable")


def measured_rootfs_digest(path: Path, *, expected_uid: int = 0) -> str:
    """Canonical tree-v1 identity: paths, modes, sizes and file contents; no links/devices."""
    path = Path(path)
    _parents(path, expected_uid)
    total = 0
    count = 0
    digest = hashlib.sha256(b"cptr-rootfs-tree/1\0")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC

    def visit(fd, rel):
        nonlocal total, count
        before = os.fstat(fd)
        _metadata(before, expected_uid)
        count += 1
        if count > MAX_ROOTFS_ENTRIES:
            raise SandboxRuntimeUnavailable("qualified rootfs exceeds entry bound")
        mode = stat.S_IMODE(before.st_mode)
        if stat.S_ISDIR(before.st_mode):
            record = [rel, "directory", mode]
            digest.update(json.dumps(record, separators=(",", ":")).encode() + b"\n")
            for name in sorted(os.listdir(fd)):
                child = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not (stat.S_ISDIR(child.st_mode) or stat.S_ISREG(child.st_mode)):
                    raise SandboxRuntimeUnavailable("qualified rootfs contains a link or special file")
                child_fd = os.open(name, flags | (os.O_DIRECTORY if stat.S_ISDIR(child.st_mode) else os.O_NONBLOCK), dir_fd=fd)
                try:
                    opened = os.fstat(child_fd)
                    if (child.st_dev, child.st_ino) != (opened.st_dev, opened.st_ino):
                        raise SandboxRuntimeUnavailable("qualified rootfs changed while opening")
                    visit(child_fd, rel + "/" + name)
                finally:
                    os.close(child_fd)
        elif stat.S_ISREG(before.st_mode):
            if before.st_nlink != 1:
                raise SandboxRuntimeUnavailable("qualified rootfs contains a hard link")
            total += before.st_size
            if total > MAX_ROOTFS_BYTES:
                raise SandboxRuntimeUnavailable("qualified rootfs exceeds byte bound")
            content = hashlib.sha256()
            read = 0
            while True:
                block = os.read(fd, min(65536, before.st_size - read + 1))
                if not block:
                    break
                read += len(block)
                if read > before.st_size:
                    raise SandboxRuntimeUnavailable("qualified rootfs changed while reading")
                content.update(block)
            if read != before.st_size:
                raise SandboxRuntimeUnavailable("qualified rootfs changed while reading")
            digest.update(json.dumps([rel, "file", mode, read, content.hexdigest()], separators=(",", ":")).encode() + b"\n")
        else:
            raise SandboxRuntimeUnavailable("qualified rootfs contains a special file")
        after = os.fstat(fd)
        if (before.st_mtime_ns, before.st_ctime_ns, before.st_size) != (after.st_mtime_ns, after.st_ctime_ns, after.st_size):
            raise SandboxRuntimeUnavailable("qualified rootfs changed during measurement")

    try:
        fd = os.open(path, flags | os.O_DIRECTORY)
        try:
            visit(fd, ".")
        finally:
            os.close(fd)
    except OSError as exc:
        raise SandboxRuntimeUnavailable("qualified rootfs cannot be measured safely") from exc
    return "sha256:" + digest.hexdigest()


def trusted_runsc_release(path: Path, *, expected_uid: int = 0) -> str:
    path = Path(path)
    try:
        _parents(path, expected_uid)
        st = path.lstat()
        _metadata(st, expected_uid)
        if not stat.S_ISREG(st.st_mode) or not os.access(path, os.X_OK):
            raise SandboxRuntimeUnavailable("qualified runsc must be an executable regular file")
        # Only server-owned immutable bytes may reach this subprocess.
        with __import__("tempfile").TemporaryFile() as output:
            subprocess.run([str(path), "--version"], stdin=subprocess.DEVNULL,
                           stdout=output, stderr=subprocess.DEVNULL, timeout=3, check=True)
            output.seek(0)
            raw = output.read(4097)
        if len(raw) > 4096:
            raise SandboxRuntimeUnavailable("qualified runsc version exceeds output bound")
        lines = raw.decode("utf-8").splitlines()
        if not lines or not lines[0].startswith("runsc version "):
            raise SandboxRuntimeUnavailable("qualified runsc returned an invalid version")
        release = lines[0][len("runsc version "):].strip()
        if not release:
            raise SandboxRuntimeUnavailable("qualified runsc returned an empty version")
        return release
    except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
        raise SandboxRuntimeUnavailable("qualified runsc release cannot be verified") from exc
