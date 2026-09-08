import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BUILDER = REPO / "scripts" / "build_sandbox_broker.py"


class CapabilityOsSandboxPackageTests(unittest.TestCase):
    def test_builder_creates_standalone_bounded_zipapp(self):
        with tempfile.TemporaryDirectory() as value:
            output = Path(value) / "broker.pyz"
            completed = subprocess.run([sys.executable, str(BUILDER), "--output", str(output)],
                                       cwd=REPO, check=True, capture_output=True, text=True)
            result = json.loads(completed.stdout)
            self.assertTrue(output.is_file())
            self.assertEqual(result["sha256"], __import__("hashlib").sha256(output.read_bytes()).hexdigest())
            with zipfile.ZipFile(output) as zf:
                names = set(zf.namelist())
            self.assertIn("__main__.py", names)
            self.assertIn("cptr/services/capability_os/sandbox_server.py", names)
            self.assertIn("cptr/services/capability_os/gvisor_dispatcher.py", names)
            self.assertNotIn("cptr/app.py", names)
            smoke = subprocess.run([sys.executable, str(output), "--self-test"], check=True,
                                   capture_output=True, text=True)
            status = json.loads(smoke.stdout)
            self.assertEqual(status["brokerProtocol"], "cptr-sandbox/2")
            self.assertEqual(
                status["supportedBrokerProtocols"], ["cptr-sandbox/1", "cptr-sandbox/2"]
            )
            self.assertEqual(status["executionSource"], "standalone-zipapp")


if __name__ == "__main__":
    unittest.main()
