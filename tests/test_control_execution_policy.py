import unittest

from cptr.utils.tools import _control_execution_policy_violation, execute_tool, get_tool_list


class ControlExecutionPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_policy_blocks_observed_install_and_network_escape_paths(self):
        context = {
            "execution_policy": {
                "allow_file_writes": False,
                "allow_commands": True,
                "allow_network": False,
                "allow_package_install": False,
            }
        }

        denied = {
            "npm install --no-audit": "package installation",
            "pip install -e .": "package installation",
            "uv sync": "package installation",
            "curl https://example.com": "external command execution",
            "git fetch origin": "external command execution",
            "ssh aws true": "external command execution",
        }
        for command, reason in denied.items():
            with self.subTest(command=command):
                result = _control_execution_policy_violation(
                    "run_command", {"command": command}, context
                )
                self.assertIsNotNone(result)
                self.assertIn(reason, result)

        for command in ("npm test", "pytest tests/", "git status"):
            with self.subTest(command=command):
                self.assertIsNone(
                    _control_execution_policy_violation(
                        "run_command", {"command": command}, context
                    )
                )

    def test_policy_blocks_file_and_browser_tools(self):
        context = {
            "execution_policy": {
                "allow_file_writes": False,
                "allow_commands": True,
                "allow_network": False,
                "allow_package_install": False,
            }
        }
        self.assertIn(
            "file writes",
            _control_execution_policy_violation("write_file", {"path": "x"}, context),
        )
        self.assertIn(
            "network access",
            _control_execution_policy_violation("web_search", {"query": "x"}, context),
        )
        self.assertIn(
            "network access",
            _control_execution_policy_violation(
                "browser_navigate", {"url": "https://example.com"}, context
            ),
        )
        self.assertIn(
            "network access",
            _control_execution_policy_violation("image_generate", {"prompt": "x"}, context),
        )
        self.assertIn(
            "network access",
            _control_execution_policy_violation("delegate_task", {"prompt": "x"}, context),
        )

    async def test_execute_tool_enforces_policy_before_dispatch(self):
        context = {
            "workspace": "/tmp/cptr-policy-test",
            "builtin_tools": None,
            "execution_policy": {
                "allow_file_writes": False,
                "allow_commands": True,
                "allow_network": False,
                "allow_package_install": False,
            },
        }
        install = await execute_tool(
            "run_command", {"command": "pip install -e ."}, context
        )
        self.assertIn("denies package installation", install)
        write = await execute_tool(
            "write_file", {"path": "blocked.txt", "content": "blocked"}, context
        )
        self.assertIn("denies file writes", write)
        external = await execute_tool("external_connector_tool", {}, context)
        self.assertIn("denies external tool servers", external)

    async def test_policy_removes_disallowed_tools_from_model_schema(self):
        tools = await get_tool_list(
            workspace="/tmp/cptr-policy-test",
            execution_policy={
                "allow_file_writes": False,
                "allow_commands": False,
                "allow_network": False,
                "allow_package_install": False,
            },
        )
        names = {tool["name"] for tool in tools}
        self.assertNotIn("write_file", names)
        self.assertNotIn("edit_file", names)
        self.assertNotIn("run_command", names)
        self.assertNotIn("web_search", names)
        self.assertNotIn("read_url", names)
        self.assertTrue({"read_file", "list_directory", "search_files"}.issubset(names))
