import unittest

from inbox import github

HEAD = "a" * 40


class AgentReviewBodyLeadTests(unittest.TestCase):
    def test_without_lead_the_notice_stays_first(self):
        body = github.agent_review_body(HEAD, "summary")
        lines = [ln for ln in body.splitlines() if ln.strip()]
        self.assertEqual(lines[0], github.AGENT_REVIEW_NOTICE)
        self.assertIn("Head: " + chr(96) + HEAD + chr(96), body)

    def test_mention_keeps_first_line_and_verdict_is_second(self):
        lead = "@QqOppieXYZ 请看一眼这条新 head" + chr(10) + "结论：要改"
        body = github.agent_review_body(HEAD, "summary", lead=lead)
        lines = [ln for ln in body.splitlines() if ln.strip()]
        self.assertEqual(lines[0], "@QqOppieXYZ 请看一眼这条新 head")
        self.assertEqual(lines[1], "结论：要改")
        self.assertIn(github.AGENT_REVIEW_NOTICE, body)
        self.assertIn("Head: " + chr(96) + HEAD + chr(96), body)

    def test_verdict_only_lead_is_the_first_line(self):
        body = github.agent_review_body(HEAD, "summary", lead="结论：通过")
        lines = [ln for ln in body.splitlines() if ln.strip()]
        self.assertEqual(lines[0], "结论：通过")
        self.assertIn(github.AGENT_REVIEW_NOTICE, body)

    def test_lead_requires_a_full_head(self):
        with self.assertRaises(ValueError):
            github.agent_review_body("abc", "summary", lead="结论：通过")


if __name__ == "__main__":
    unittest.main()
