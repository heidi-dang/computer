"""Regression guards for Heidi's native CPTR transcript bridge.

The frontend package does not currently ship a browser/unit-test runner. These
source-level guards protect the event lifecycle contract until one is added.
"""

from pathlib import Path
import unittest


CHAT_PANEL = (
    Path(__file__).resolve().parents[1]
    / "cptr"
    / "frontend"
    / "src"
    / "lib"
    / "components"
    / "chat"
    / "ChatPanel.svelte"
)
CHAT_INPUT = CHAT_PANEL.with_name("ChatInput.svelte")
STATUS_STRIP = CHAT_PANEL.with_name("FlowDeckStatusStrip.svelte")


class FlowDeckNativeTranscriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = CHAT_PANEL.read_text()
        cls.chat_input_source = CHAT_INPUT.read_text()
        cls.status_strip_source = STATUS_STRIP.read_text()

    def test_normal_and_flowdeck_paths_share_native_handler(self):
        self.assertGreaterEqual(
            self.source.count("applyNativeTranscriptEvent(data, msg)"), 1
        )
        self.assertGreaterEqual(
            self.source.count("applyNativeTranscriptEvent(event, message)"), 1
        )
        self.assertIn("if (data.error) toast.error(data.error", self.source)
        self.assertIn("if (!data.done) return;", self.source)

    def test_native_completion_clears_streaming_without_claiming_durable_success(self):
        self.assertIn("msg.done = true;", self.source)
        self.assertIn("flowdeckStatus = data.error ? 'failed' : 'verifying';", self.source)
        self.assertIn("durable FlowDeck", self.source)

    def test_native_output_items_deduplicate_tool_updates(self):
        self.assertIn("o.type === itemType && o.call_id === callId", self.source)
        self.assertIn("existing[existingIdx] = { ...existing[existingIdx], ...data.output }", self.source)

    def test_polling_normalizes_durable_run_identity_and_is_reconciliation_only(self):
        self.assertIn("event?.run_id", self.source)
        self.assertIn("{ ...event, flowdeck_parent_run_id: event.run_id }", self.source)
        self.assertIn("getFlowDeckOrchestration(runId, workspace)", self.source)
        self.assertIn("startFlowDeckPolling(flowdeckRunId)", self.source)

    def test_flowdeck_lifecycle_keeps_terminal_reconciliation(self):
        for kind in ("RUN_COMPLETED", "RUN_CANCELLED", "RUN_ORPHANED", "RUN_FAILED"):
            self.assertIn(f"kind === '{kind}'", self.source)
        self.assertIn("message.done = true;", self.source)
        self.assertIn("stopFlowDeckPolling();", self.source)

    def test_parent_identity_is_used_for_child_native_events(self):
        self.assertIn("flowdeck_parent_run_id?: string", self.source)
        self.assertIn("const parentRunId = data.flowdeck_parent_run_id || data.flowdeck_run_id", self.source)
        self.assertIn("parentRunId === flowdeckRunId", self.source)

    def test_child_completion_cannot_terminalize_parent_native_transcript(self):
        self.assertIn("const isChildFlowDeckCompletion =", self.source)
        self.assertIn("data.flowdeck_parent_run_id !== data.flowdeck_run_id", self.source)
        self.assertIn("if (isChildFlowDeckCompletion)", self.source)
        self.assertIn("only the", self.source)
        self.assertIn("parent run may end native streaming", self.source)

    def test_waiting_feedback_is_immediate_and_replaced_by_backend_activity(self):
        self.assertIn("flowdeckStatus = 'preparing';", self.source)
        self.assertIn("Preparing FlowDeck…", self.status_strip_source)
        self.assertIn("flowDeckStatusForEvent", self.source)
        self.assertIn("if (reportedStatus) flowdeckStatus = reportedStatus;", self.source)

    def test_status_strip_is_outside_composer_and_terminal_states_are_static(self):
        self.assertIn("<FlowDeckStatusStrip", self.source)
        self.assertNotIn("FlowDeckStatusStrip", self.chat_input_source)
        for status in (
            "cancelled",
            "succeeded",
            "failed",
            "unknown",
            "manual_review",
            "manual_review_required",
            "orphaned",
        ):
            self.assertIn(f"'{status}'", self.status_strip_source)
        self.assertIn("class:is-terminal={isTerminal}", self.status_strip_source)
        self.assertIn("const isActive = $derived", self.status_strip_source)
        self.assertNotIn("flowdeck-composer-active", self.chat_input_source)
        self.assertNotIn("flowdeck-pulse", self.chat_input_source)
        self.assertNotIn("flowdeck-breathe", self.chat_input_source)


if __name__ == "__main__":
    unittest.main()