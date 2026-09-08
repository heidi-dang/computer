import io
import json
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from cptr.services.capability_os.forge import ContentAddressedBlobStore
from cptr.services.capability_os.mcp_package import (
    BRIDGE_ENTRYPOINT,
    McpbPackageError,
    McpbPackagePreparer,
)


def _manifest(**overrides):
    value = {
        "manifest_version": "0.4",
        "name": "io.example/text-python",
        "version": "1.2.3",
        "description": "fixture",
        "server": {
            "type": "python",
            "entry_point": "server/main.py",
            "mcp_config": {
                "command": "python3",
                "args": ["${__dirname}/server/main.py"],
                "env": {},
            },
        },
        "compatibility": {"platforms": ["linux"]},
    }
    value.update(overrides)
    return value


def _archive(*, manifest=None, files=None, symlink=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest or _manifest()))
        for name, content in (files or {"server/main.py": "print('fixture')\n"}).items():
            archive.writestr(name, content)
        if symlink is not None:
            info = zipfile.ZipInfo("server/link.py")
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, symlink)
    return stream.getvalue()


class McpbPackagePreparerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.blobs = ContentAddressedBlobStore(Path(self.tmp.name) / "blobs")
        self.preparer = McpbPackagePreparer(blobs=self.blobs)

    def tearDown(self):
        self.tmp.cleanup()

    def test_self_contained_text_python_mcpb_becomes_immutable_sandbox_bundle(self):
        prepared = self.preparer.prepare(_archive(), expected_version="1.2.3")

        self.assertTrue(prepared.bundle_digest.startswith("sha256:"))
        self.assertEqual(prepared.manifest_version, "0.4")
        self.assertEqual(prepared.package_name, "io.example/text-python")
        self.assertEqual(prepared.server_entrypoint, "server/main.py")
        files = self.blobs.get_files(prepared.bundle_digest)
        self.assertEqual(files["server/main.py"], "print('fixture')\n")
        self.assertIn(BRIDGE_ENTRYPOINT, files)
        bridge = files[BRIDGE_ENTRYPOINT]
        self.assertIn('"PATH": "/usr/bin:/bin"', bridge)
        self.assertNotIn("os.environ.copy", bridge)
        self.assertIn("start_new_session=True", bridge)
        self.assertIn('mode not in {"probe", "call"}', bridge)

    def test_rejects_traversal_symlink_binary_and_reserved_bridge(self):
        invalid_archives = []

        stream = io.BytesIO()
        with zipfile.ZipFile(stream, mode="w") as archive:
            archive.writestr("manifest.json", json.dumps(_manifest()))
            archive.writestr("../escape.py", "print(1)\n")
            archive.writestr("server/main.py", "print(1)\n")
        invalid_archives.append(stream.getvalue())
        invalid_archives.append(_archive(symlink="main.py"))
        invalid_archives.append(_archive(files={"server/main.py": b"\xff\xfe"}))
        invalid_archives.append(
            _archive(
                files={
                    "server/main.py": "print(1)\n",
                    BRIDGE_ENTRYPOINT: "attacker controlled",
                }
            )
        )

        for content in invalid_archives:
            with self.subTest(size=len(content)):
                with self.assertRaises(McpbPackageError):
                    self.preparer.prepare(content, expected_version="1.2.3")

    def test_rejects_install_runtime_credentials_and_unresolved_configuration(self):
        node = _manifest(server={"type": "node", "entry_point": "server/main.js"})
        env = _manifest(
            server={
                "type": "python",
                "entry_point": "server/main.py",
                "mcp_config": {"command": "python3", "env": {"TOKEN": "${user_config.token}"}},
            }
        )
        required_config = _manifest(
            user_config={"token": {"type": "string", "required": True}},
        )
        unresolved = _manifest(
            server={
                "type": "python",
                "entry_point": "server/main.py",
                "mcp_config": {
                    "command": "python3",
                    "args": ["server/main.py", "${user_config.path}"],
                    "env": {},
                },
            }
        )

        for manifest in (node, env, required_config, unresolved):
            with self.subTest(server=manifest.get("server")):
                with self.assertRaises(McpbPackageError):
                    self.preparer.prepare(_archive(manifest=manifest), expected_version="1.2.3")

    def test_rejects_registry_manifest_version_mismatch(self):
        with self.assertRaises(McpbPackageError):
            self.preparer.prepare(_archive(), expected_version="9.9.9")


if __name__ == "__main__":
    unittest.main()
