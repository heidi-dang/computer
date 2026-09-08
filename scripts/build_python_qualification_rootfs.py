#!/usr/bin/env python3
"""Build a minimal Python qualification rootfs from the installed host runtime.
This creates a candidate only. Root-owned installation and live qualification are separate.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path


def _seal_candidate(destination: Path) -> None:
    """Make a built candidate immutable to its non-root builder identity.

    Qualification may execute the relocated interpreter before root-owned
    installation.  If candidate directories remain owner-writable, CPython can
    create ``__pycache__`` files and silently change the measured tree digest.
    Seal every file and directory after construction so qualification is a
    read-only operation and the content-addressed identity stays stable.
    """
    for current, _dirs, files in os.walk(destination, topdown=False):
        directory = Path(current)
        for name in files:
            path = directory / name
            mode = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o555 if mode & 0o111 else 0o444)
        directory.chmod(0o555)


def build(destination):
    destination = Path(destination).absolute()
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    copied = set()

    def copy_file(source, relative=None):
        source = Path(source)
        target = destination / (relative or str(source).lstrip("/"))
        if str(target) in copied:
            return
        real = source.resolve(strict=True)
        if not real.is_file():
            raise ValueError("runtime input is not a regular file")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real, target)
        target.chmod(0o755 if os.access(real, os.X_OK) else 0o644)
        copied.add(str(target))
        # ldd is used only on trusted, installed host ELF binaries.
        with real.open("rb") as handle:
            elf = handle.read(4) == b"\x7fELF"
        if elf:
            out = subprocess.run(["ldd", str(real)], capture_output=True, text=True, check=True, timeout=5).stdout
            if "not found" in out:
                raise RuntimeError("host runtime dependency is missing")
            for line in out.splitlines():
                match = re.search(r"(?:=>\s+)?(/[^\s]+)", line)
                if match:
                    copy_file(Path(match.group(1)))

    copy_file(Path(sys.executable), "usr/bin/python3")
    standard = Path(sysconfig.get_path("stdlib")).resolve(strict=True)
    # The interpreter is intentionally relocated to /usr/bin inside the
    # immutable rootfs. Relocate its stdlib coherently under /usr as well so
    # CPython discovers the runtime prefix from the executable location. Do
    # not preserve the host installation path (for example a uv-managed
    # ~/.local/share path), which is absent inside the sandbox.
    stdlib_target = Path("usr/lib") / f"python{sys.version_info.major}.{sys.version_info.minor}"
    # The qualification profile is intentionally headless.  CPython builds may
    # ship _tkinter even when the corresponding Tcl/Tk shared libraries are not
    # installed on the host (uv standalone Python is one example).  Including
    # that optional GUI module makes the immutable runtime closure impossible to
    # verify with ldd even though Capability OS never needs a display server.
    excluded = {"__pycache__", "site-packages", "dist-packages", "test", "tests",
                "idlelib", "turtledemo", "ensurepip", "tkinter"}
    for current, dirs, files in os.walk(standard, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in excluded and not d.startswith("config-") and not (Path(current) / d).is_symlink())
        for name in sorted(files):
            path = Path(current) / name
            if (path.is_symlink() or name.endswith((".pyc", ".a", ".o"))
                    or name.startswith("_tkinter.")):
                continue
            if path.is_file():
                copy_file(path, stdlib_target / path.relative_to(standard))
    for path in ("proc", "dev", "sys", "tmp", "work", "etc"):
        (destination / path).mkdir(mode=0o755, exist_ok=True)
    (destination / "etc/passwd").write_text("nobody:x:65534:65534:Sandbox:/tmp:/nonexistent\n")
    (destination / "etc/group").write_text("nogroup:x:65534:\n")
    _seal_candidate(destination)
    result = {"rootfs": str(destination), "pythonVersion": "Python " + sys.version.split()[0],
              "profileVersion": "cptr-python-gvisor/2", "source": "installed-host-python",
              "files": len(copied)}
    print(json.dumps(result, sort_keys=True))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    build(parser.parse_args().output)
