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


class LeadValidationTests(unittest.TestCase):
    def test_two_mentions_on_the_first_line_are_refused(self):
        with self.assertRaises(ValueError):
            github.agent_review_body(HEAD, "body", lead="@AOppieXYZ @BOppieXYZ look" + chr(10) + "结论：通过")

    def test_mention_without_a_verdict_line_is_refused(self):
        with self.assertRaises(ValueError):
            github.agent_review_body(HEAD, "body", lead="@AOppieXYZ look")

    def test_verdict_only_lead_is_trimmed(self):
        body = github.agent_review_body(HEAD, "body", lead="  结论：通过  ")
        self.assertEqual([ln for ln in body.splitlines() if ln.strip()][0], "结论：通过")

    def test_verdict_lead_with_extra_lines_is_refused(self):
        with self.assertRaises(ValueError):
            github.agent_review_body(HEAD, "body", lead="结论：通过" + chr(10) + "extra")


if __name__ == "__main__":
    unittest.main()
