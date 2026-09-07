import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_python_qualification_rootfs import build


class CapabilityOsPythonRootfsBuilderTests(unittest.TestCase):
    def test_relocated_interpreter_discovers_relocated_stdlib_in_isolated_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            rootfs = Path(tmp) / "rootfs"
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                build(rootfs)
            metadata = json.loads(output.getvalue())
            self.assertEqual(metadata["profileVersion"], "cptr-python-gvisor/2")

            version = f"python{sys.version_info.major}.{sys.version_info.minor}"
            stdlib = rootfs / "usr" / "lib" / version
            self.assertTrue((stdlib / "encodings" / "__init__.py").is_file())
            self.assertFalse((rootfs / str(Path(sys.prefix).resolve()).lstrip("/")).exists())

            probe = subprocess.run(
                [
                    str(rootfs / "usr" / "bin" / "python3"),
                    "-I",
                    "-c",
                    "import encodings,sys; print(sys.prefix); print(encodings.__file__)",
                ],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            lines = probe.stdout.splitlines()
            self.assertEqual(Path(lines[0]).resolve(), (rootfs / "usr").resolve())
            self.assertTrue(Path(lines[1]).resolve().is_relative_to(stdlib.resolve()))


if __name__ == "__main__":
    unittest.main()
