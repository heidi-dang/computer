import unittest
from unittest.mock import AsyncMock, patch

from cptr.utils import memory as managed_memory


class MemorySettingsCacheTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        managed_memory._memory_settings_cache = None

    async def test_settings_load_uses_one_namespace_query_and_reuses_short_cache(self):
        loader = AsyncMock(
            return_value={
                "memory.enabled": True,
                "memory.required_for_execution": True,
                "memory.context_char_limit": 5000,
            }
        )
        with patch.object(managed_memory.Config, "get_namespace", new=loader):
            first = await managed_memory.get_memory_settings(force_refresh=True)
            second = await managed_memory.get_memory_settings()

        self.assertEqual(loader.await_count, 1)
        self.assertEqual(first["context_char_limit"], 5000)
        self.assertEqual(second, first)

    async def test_save_invalidates_cache_and_reloads_namespace(self):
        loader = AsyncMock(
            side_effect=[
                {"memory.enabled": True},
                {"memory.enabled": False},
            ]
        )
        upsert = AsyncMock()
        with (
            patch.object(managed_memory.Config, "get_namespace", new=loader),
            patch.object(managed_memory.Config, "upsert", new=upsert),
        ):
            before = await managed_memory.get_memory_settings(force_refresh=True)
            after = await managed_memory.save_memory_settings({"enabled": False})

        self.assertTrue(before["enabled"])
        self.assertFalse(after["enabled"])
        self.assertEqual(loader.await_count, 2)
        upsert.assert_awaited_once_with({"memory.enabled": False})


if __name__ == "__main__":
    unittest.main()
