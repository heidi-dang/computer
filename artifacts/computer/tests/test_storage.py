import tempfile
import unittest
from pathlib import Path

from cptr.utils.storage import LocalStorage


class LocalStorageContainmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "uploads"
        self.outside = Path(self.temp.name) / "outside.txt"
        self.outside.write_text("sentinel")
        self.storage = LocalStorage(self.root)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_traversal_and_absolute_keys_are_rejected(self):
        for key in ("../outside.txt", "nested/../../outside.txt", str(self.outside)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                await self.storage.get(key)
            with self.subTest(operation="put", key=key), self.assertRaises(ValueError):
                await self.storage.put(key, b"modified")
            with self.subTest(operation="delete", key=key), self.assertRaises(ValueError):
                await self.storage.delete(key)
        self.assertEqual(self.outside.read_text(), "sentinel")

    async def test_symlinked_upload_path_cannot_escape_root(self):
        link = self.root / "linked"
        link.symlink_to(self.outside)
        with self.assertRaises(ValueError):
            await self.storage.get("linked")
        self.assertEqual(self.outside.read_text(), "sentinel")

    async def test_valid_key_remains_available(self):
        await self.storage.put("valid-file-id", b"payload")
        self.assertEqual(await self.storage.get("valid-file-id"), b"payload")
        await self.storage.delete("valid-file-id")
        self.assertIsNone(await self.storage.get("valid-file-id"))