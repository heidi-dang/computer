import unittest

from cptr.utils.tools import execute_tool


class AssignmentScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_narrow_scope_denies_unlisted_file(self):
        result = await execute_tool(
            "read_file",
            {"path": "historical-target.txt"},
            {
                "workspace": "/tmp/disposable-workspace",
                "inspection_scope": "assignment",
                "assignment_paths": ["fresh-target.txt"],
            },
        )

        self.assertIn("inspection scope violation", result)
        self.assertIn("historical-target.txt", result)

    async def test_narrow_scope_denies_workspace_wide_listing(self):
        result = await execute_tool(
            "list_directory",
            {"path": ".", "recursive": True},
            {
                "workspace": "/tmp/disposable-workspace",
                "inspection_scope": "assignment",
                "assignment_paths": ["fresh-target.txt"],
            },
        )

        self.assertIn("inspection scope violation", result)

    async def test_default_workspace_mode_preserves_broad_investigation(self):
        result = await execute_tool(
            "list_directory",
            {"path": ".", "recursive": False},
            {
                "workspace": "/tmp/disposable-workspace",
                "inspection_scope": "workspace",
            },
        )

        self.assertNotIn("inspection scope violation", result)
        self.assertIn("request context unavailable", result)


if __name__ == "__main__":
    unittest.main()
