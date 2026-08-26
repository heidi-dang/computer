import unittest

from cptr.services.task_integrity import (
    COMPLETE_WITH_TOOL_ERRORS,
    completion_integrity,
    successful_terminal_status,
    tool_error_count,
)


class TaskIntegrityTests(unittest.TestCase):
    def test_detects_failed_function_call_output(self):
        output = [
            {
                "type": "function_call",
                "call_id": "call-1",
                "name": "list_directory",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Error: inspection scope violation: assignment scope has no allowed paths",
            },
        ]

        self.assertEqual(tool_error_count(output), 1)
        self.assertEqual(successful_terminal_status(output), COMPLETE_WITH_TOOL_ERRORS)
        self.assertEqual(
            completion_integrity(output),
            {"status": "TOOL_ERRORS", "tool_error_count": 1},
        )

    def test_deduplicates_failed_call_and_failed_output(self):
        output = [
            {"type": "function_call", "call_id": "call-1", "status": "failed"},
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": {"status": "error", "error": "blocked"},
            },
        ]
        self.assertEqual(tool_error_count(output), 1)

    def test_detects_structured_json_failure_output(self):
        output = [
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": '{"success": false, "error": "command rejected"}',
            }
        ]
        self.assertEqual(tool_error_count(output), 1)

    def test_ordinary_assistant_error_text_is_not_tool_failure(self):
        output = [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Error handling is implemented."}],
            },
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "Error handling is implemented.",
            },
        ]
        self.assertEqual(tool_error_count(output), 0)
        self.assertEqual(successful_terminal_status(output), "COMPLETE")
        self.assertEqual(completion_integrity(output), {"status": "CLEAN", "tool_error_count": 0})


if __name__ == "__main__":
    unittest.main()
