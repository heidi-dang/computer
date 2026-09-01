import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from cptr.services.mcp_topology_config import (
    CANONICAL_TOPOLOGY_LABELS,
    get_topology_config,
    sanitize_topology_alias,
    sanitize_topology_node_id,
    update_topology_aliases,
)


class McpTopologyConfigServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_canonical_labels_and_sanitizers_are_strict(self):
        self.assertEqual(
            CANONICAL_TOPOLOGY_LABELS,
            {
                "mcp-connector": "MCP Connector",
                "cptr-mcp": "CPTR MCP",
                "cptr-backend": "CPTR Backend",
            },
        )
        self.assertEqual(
            sanitize_topology_alias("  Workstation   Backend  "), "Workstation Backend"
        )
        self.assertIsNone(sanitize_topology_alias("   "))
        with self.assertRaises(ValueError):
            sanitize_topology_alias("x" * 81)
        with self.assertRaises(ValueError):
            sanitize_topology_alias("bad\x00alias")
        with self.assertRaises(ValueError):
            sanitize_topology_node_id("../bad")

    async def test_config_reads_only_valid_aliases(self):
        with patch(
            "cptr.services.mcp_topology_config.Config.get",
            AsyncMock(
                return_value={
                    "cptr-backend": "  Workstation  ",
                    "../bad": "ignored",
                    "mcp-connector": "   ",
                }
            ),
        ):
            result = await get_topology_config()

        self.assertEqual(result["version"], 1)
        self.assertEqual(result["canonical_labels"], CANONICAL_TOPOLOGY_LABELS)
        self.assertEqual(result["aliases"], {"cptr-backend": "Workstation"})

    async def test_partial_update_merges_and_reset_removes_only_target_alias(self):
        get = AsyncMock(return_value={"mcp-connector": "Gateway", "cptr-backend": "Old Backend"})
        upsert = AsyncMock()
        with (
            patch("cptr.services.mcp_topology_config.Config.get", get),
            patch("cptr.services.mcp_topology_config.Config.upsert", upsert),
        ):
            updated = await update_topology_aliases({"cptr-backend": "Workstation"})

        upsert.assert_awaited_once_with(
            {
                "mcp.topology.aliases": {
                    "mcp-connector": "Gateway",
                    "cptr-backend": "Workstation",
                }
            }
        )
        self.assertEqual(updated["aliases"]["cptr-backend"], "Workstation")

        get = AsyncMock(return_value={"mcp-connector": "Gateway", "cptr-backend": "Workstation"})
        upsert = AsyncMock()
        with (
            patch("cptr.services.mcp_topology_config.Config.get", get),
            patch("cptr.services.mcp_topology_config.Config.upsert", upsert),
        ):
            reset = await update_topology_aliases({"cptr-backend": None})

        upsert.assert_awaited_once_with({"mcp.topology.aliases": {"mcp-connector": "Gateway"}})
        self.assertEqual(reset["aliases"], {"mcp-connector": "Gateway"})

    async def test_update_rejects_invalid_node_ids_and_alias_values(self):
        with self.assertRaises(ValueError):
            await update_topology_aliases({"../bad": "Alias"})
        with self.assertRaises(ValueError):
            await update_topology_aliases({"cptr-backend": "x" * 81})


class McpTopologyConfigRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_and_put_are_admin_only_and_put_forwards_partial_aliases(self):
        from cptr.routers import mcp as mcp_router

        request = SimpleNamespace(cookies={}, client=None)
        admin = Mock(return_value=SimpleNamespace(user_id="admin-1"))
        get_config = AsyncMock(
            return_value={
                "version": 1,
                "canonical_labels": CANONICAL_TOPOLOGY_LABELS,
                "aliases": {},
            }
        )
        update = AsyncMock(
            return_value={
                "version": 1,
                "canonical_labels": CANONICAL_TOPOLOGY_LABELS,
                "aliases": {"cptr-backend": "Workstation"},
            }
        )
        body = mcp_router.McpTopologyConfigUpdate(aliases={"cptr-backend": "Workstation"})

        with (
            patch.object(mcp_router, "require_admin", admin),
            patch.object(mcp_router, "get_topology_config", get_config),
            patch.object(mcp_router, "update_topology_aliases", update),
        ):
            result_get = await mcp_router.get_mcp_topology_config(request)
            result_put = await mcp_router.put_mcp_topology_config(request, body)

        self.assertEqual(admin.call_count, 2)
        get_config.assert_awaited_once()
        update.assert_awaited_once_with({"cptr-backend": "Workstation"})
        self.assertEqual(result_get["aliases"], {})
        self.assertEqual(result_put["aliases"], {"cptr-backend": "Workstation"})

    def test_update_body_forbids_unknown_fields(self):
        from pydantic import ValidationError
        from cptr.routers import mcp as mcp_router

        with self.assertRaises(ValidationError):
            mcp_router.McpTopologyConfigUpdate.model_validate({"aliases": {}, "unexpected": True})


if __name__ == "__main__":
    unittest.main()
