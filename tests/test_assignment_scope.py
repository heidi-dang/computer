import unittest

from cptr.utils.tools import _assignment_scope_violation, execute_tool, get_tool_list


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

    def test_assignment_scope_allows_pathless_bounded_wait(self):
        context = {
            "workspace": "/tmp/disposable-workspace",
            "inspection_scope": "assignment",
            "assignment_paths": ["fresh-target.txt"],
        }

        self.assertIsNone(
            _assignment_scope_violation(
                "run_command",
                {"command": "sleep 20", "cwd": "."},
                context,
            )
        )

    def test_assignment_scope_allows_literal_pathless_output(self):
        context = {
            "workspace": "/tmp/disposable-workspace",
            "inspection_scope": "assignment",
            "assignment_paths": ["fresh-target.txt"],
        }

        self.assertIsNone(
            _assignment_scope_violation(
                "run_command",
                {"command": "printf 'waiting\\n'", "cwd": "."},
                context,
            )
        )

    def test_assignment_scope_still_denies_unlisted_run_command_access(self):
        context = {
            "workspace": "/tmp/disposable-workspace",
            "inspection_scope": "assignment",
            "assignment_paths": ["fresh-target.txt"],
        }

        result = _assignment_scope_violation(
            "run_command",
            {"command": "cat historical-target.txt", "cwd": "."},
            context,
        )
        self.assertIn("inspection scope violation", result)

    def test_assignment_scope_allows_named_file_read_but_rejects_discovery(self):
        context = {
            "workspace": "/tmp/disposable-workspace",
            "inspection_scope": "assignment",
            "assignment_paths": ["assignment_target.py"],
        }

        self.assertIsNone(
            _assignment_scope_violation(
                "run_command",
                {"command": "cat assignment_target.py", "cwd": "."},
                context,
            )
        )
        self.assertIn(
            "inspection scope violation",
            _assignment_scope_violation(
                "run_command",
                {"command": "find .", "cwd": "."},
                context,
            ),
        )

    async def test_assignment_scope_denies_chat_history_search_before_execution(self):
        context = {
            "workspace": "/tmp/disposable-workspace",
            "inspection_scope": "assignment",
            "assignment_paths": ["fresh-target.txt"],
        }
        result = await execute_tool(
            "search_chats",
            {"query": "historical", "workspace_scope": "all"},
            context,
        )
        self.assertIn("inspection scope violation", result)
        self.assertIn("capability is not available", result)
        self.assertEqual(context["assignment_scope_violations"], 1)

    def test_assignment_scope_locks_authority_after_repeated_denials(self):
        context = {
            "workspace": "/tmp/disposable-workspace",
            "inspection_scope": "assignment",
            "assignment_paths": ["fresh-target.txt"],
        }
        first = _assignment_scope_violation(
            "search_chats", {"query": "first", "workspace_scope": "all"}, context
        )
        second = _assignment_scope_violation(
            "web_search", {"query": "second"}, context
        )
        locked = _assignment_scope_violation(
            "read_file", {"path": "fresh-target.txt"}, context
        )
        self.assertIn("capability is not available", first)
        self.assertIn("worker authority is locked", second)
        self.assertTrue(context["assignment_scope_locked"])
        self.assertIn("authority is locked", locked)

    async def test_assignment_scope_advertises_only_path_scoped_capabilities(self):
        tools = await get_tool_list(
            workspace="/tmp/disposable-workspace", inspection_scope="assignment"
        )
        names = {tool["name"] for tool in tools}
        self.assertIn("read_file", names)
        self.assertIn("edit_file", names)
        self.assertNotIn("search_chats", names)
        self.assertNotIn("web_search", names)
        self.assertNotIn("read_url", names)
        self.assertNotIn("browser_navigate", names)

    def test_assignment_scope_rejects_shell_escape_syntax(self):
        context = {
            "workspace": "/tmp/disposable-workspace",
            "inspection_scope": "assignment",
            "assignment_paths": ["assignment_target.py"],
        }

        result = _assignment_scope_violation(
            "run_command",
            {"command": "cat assignment_target.py; cat historical-target.txt", "cwd": "."},
            context,
        )
        self.assertIn("cannot be proven assignment-scoped", result)


if __name__ == "__main__":
    unittest.main()
