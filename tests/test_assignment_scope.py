import unittest

from cptr.utils.tools import _assignment_scope_violation, execute_tool


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
