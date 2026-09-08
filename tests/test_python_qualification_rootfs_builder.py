import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

from cptr.services.capability_os.runtime_identity import measured_rootfs_digest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_python_qualification_rootfs.py"
SPEC = importlib.util.spec_from_file_location("build_python_qualification_rootfs", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class PythonQualificationRootfsBuilderTests(unittest.TestCase):
    def test_sealed_candidate_is_read_only_and_digest_stable_across_python_import(self):
        with tempfile.TemporaryDirectory() as value:
            rootfs = Path(value) / "rootfs"
            lib = rootfs / "usr/lib/python-probe"
            binary = rootfs / "usr/bin/python3"
            lib.mkdir(parents=True)
            binary.parent.mkdir(parents=True)
            module = lib / "probe.py"
            module.write_text("VALUE = 7\n", encoding="utf-8")
            binary.write_text("placeholder\n", encoding="utf-8")
            binary.chmod(0o755)

            builder._seal_candidate(rootfs)

            self.assertEqual(stat.S_IMODE(rootfs.stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE(lib.stat().st_mode), 0o555)
            self.assertEqual(stat.S_IMODE(module.stat().st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o555)

            before = measured_rootfs_digest(rootfs, expected_uid=os.getuid())
            env = dict(os.environ)
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            env.pop("PYTHONPYCACHEPREFIX", None)
            env["PYTHONPATH"] = str(lib)
            completed = subprocess.run(
                [sys.executable, "-c", "import probe; print(probe.VALUE)"],
                check=True,
                capture_output=True,
                text=True,
                env=env,
                timeout=5,
            )
            self.assertEqual(completed.stdout.strip(), "7")
            self.assertFalse((lib / "__pycache__").exists())
            self.assertEqual(
                measured_rootfs_digest(rootfs, expected_uid=os.getuid()),
                before,
            )


if __name__ == "__main__":
    unittest.main()
