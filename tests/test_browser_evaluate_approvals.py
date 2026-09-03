import unittest

from cptr.services.browser_evaluate_approvals import BrowserEvaluateApprovals


class BrowserEvaluateApprovalTests(unittest.TestCase):
    def test_approval_is_single_use_and_bound_to_user_session_and_expression(self):
        approvals = BrowserEvaluateApprovals()
        approval = approvals.issue(user_id="user_1", session_id="brs_1", expression="document.title")

        self.assertFalse(approvals.consume(
            token=approval.token,
            user_id="user_1",
            session_id="brs_other",
            expression="document.title",
        ))

        approval = approvals.issue(user_id="user_1", session_id="brs_1", expression="document.title")
        self.assertFalse(approvals.consume(
            token=approval.token,
            user_id="user_1",
            session_id="brs_1",
            expression="document.cookie",
        ))

        approval = approvals.issue(user_id="user_1", session_id="brs_1", expression="document.title")
        self.assertTrue(approvals.consume(
            token=approval.token,
            user_id="user_1",
            session_id="brs_1",
            expression="document.title",
        ))
        self.assertFalse(approvals.consume(
            token=approval.token,
            user_id="user_1",
            session_id="brs_1",
            expression="document.title",
        ))


if __name__ == "__main__":
    unittest.main()
