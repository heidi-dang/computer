import json
import unittest
from pathlib import Path

from cptr.services.browser_protocol import (
    BROWSER_ACTIONS,
    MUTATING_BROWSER_ACTIONS,
    PROTOCOL_VERSION,
)


class BrowserProtocolContractTests(unittest.TestCase):
    def test_backend_matches_checked_in_cross_repo_protocol_manifest(self):
        contract_path = (
            Path(__file__).resolve().parents[1] / "contracts" / "browser-protocol-v1.json"
        )
        contract = json.loads(contract_path.read_text(encoding="utf-8"))

        self.assertEqual(contract["contract"], "cptr-browser-device")
        self.assertEqual(contract["contract_revision"], 1)
        self.assertEqual(contract["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(set(contract["browser_actions"]), BROWSER_ACTIONS)
        self.assertEqual(set(contract["mutating_browser_actions"]), MUTATING_BROWSER_ACTIONS)
        self.assertEqual(len(contract["browser_actions"]), len(BROWSER_ACTIONS))
        self.assertEqual(
            len(contract["mutating_browser_actions"]),
            len(MUTATING_BROWSER_ACTIONS),
        )


if __name__ == "__main__":
    unittest.main()
