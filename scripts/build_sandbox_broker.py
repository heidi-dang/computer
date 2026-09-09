#!/usr/bin/env python3
"""Build a standalone standard-library-only Capability OS sandbox broker zipapp."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipapp
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MODULES = (
    "sandbox_broker.py",
    "sandbox_daemon.py",
    "gvisor_dispatcher.py",
    "runtime_identity.py",
    "sandbox_server.py",
    "wasm_dispatcher.py",
)


def build(output: Path) -> dict[str, object]:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cptr-sandbox-zipapp-") as value:
        root = Path(value)
        package = root / "cptr" / "services" / "capability_os"
        package.mkdir(parents=True)
        for init in (
            root / "cptr" / "__init__.py",
            root / "cptr" / "services" / "__init__.py",
            package / "__init__.py",
        ):
            init.write_text("", encoding="utf-8")
        for name in MODULES:
            shutil.copy2(REPO / "cptr" / "services" / "capability_os" / name, package / name)
        (root / "__main__.py").write_text(
            "from cptr.services.capability_os.sandbox_server import main\nraise SystemExit(main())\n",
            encoding="utf-8",
        )
        temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
        try:
            zipapp.create_archive(
                root, target=temporary, interpreter="/usr/bin/env python3", compressed=True
            )
            os.chmod(temporary, 0o755)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {"path": str(output), "sha256": digest, "modules": list(MODULES)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    result = build(parser.parse_args().output)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
