import unittest

from inbox.review_verdict import CHANGES, DISCUSS, PASS, review_verdict_label


def f(severity="major", verify="confirmed", kind="bug", **extra):
    finding = {"severity": severity, "verify": verify}
    if kind is not None:
        finding["kind"] = kind
    finding.update(extra)
    return finding


R = lambda verdict: {"verdict": verdict}


class DecisionTableTests(unittest.TestCase):
    def test_confirmed_blocker_is_changes_needed(self):
        self.assertEqual(review_verdict_label(R("changes_needed"), [f("blocker", "confirmed")]), CHANGES)

    def test_confirmed_major_with_plausible_mix_stays_changes_needed(self):
        items = [f("major", "confirmed"), f("major", "plausible")]
        self.assertEqual(review_verdict_label(R("changes_needed"), items), CHANGES)

    def test_lgtm_with_confirmed_bug_is_still_changes_needed(self):
        self.assertEqual(review_verdict_label(R("lgtm"), [f("blocker", "confirmed")]), CHANGES)

    def test_plausible_blocking_is_discuss(self):
        self.assertEqual(review_verdict_label(R("needs_human"), [f("major", "plausible")]), DISCUSS)

    def test_needs_human_without_blocking_is_not_pass(self):
        self.assertEqual(review_verdict_label(R("needs_human"), [f("minor", "confirmed")]), DISCUSS)

    def test_changes_needed_without_blocking_is_discuss(self):
        self.assertEqual(review_verdict_label(R("changes_needed"), [f("minor", "confirmed")]), DISCUSS)

    def test_minor_only_is_pass(self):
        self.assertEqual(review_verdict_label(R("minor"), [f("minor", "confirmed")]), PASS)

    def test_no_findings_lgtm_is_pass(self):
        self.assertEqual(review_verdict_label(R("lgtm"), []), PASS)

    def test_evidence_request_does_not_block(self):
        self.assertEqual(review_verdict_label(R("minor"), [f("major", "plausible", kind="evidence")]), PASS)

    def test_rejected_finding_is_ignored(self):
        self.assertEqual(review_verdict_label(R("minor"), [f("major", "rejected")]), PASS)


class MalformedInputTests(unittest.TestCase):
    def test_unknown_verdict_is_discuss(self):
        self.assertEqual(review_verdict_label({"verdict": "ship_it"}, []), DISCUSS)

    def test_missing_verdict_is_discuss(self):
        self.assertEqual(review_verdict_label({}, []), DISCUSS)

    def test_non_dict_review_is_discuss(self):
        self.assertEqual(review_verdict_label(None, []), DISCUSS)

    def test_finding_without_severity_is_discuss(self):
        self.assertEqual(review_verdict_label(R("minor"), [{"verify": "confirmed"}]), DISCUSS)

    def test_blocking_finding_without_verify_is_discuss(self):
        self.assertEqual(review_verdict_label(R("minor"), [{"severity": "major", "kind": "bug"}]), DISCUSS)

    def test_non_mapping_finding_is_discuss(self):
        self.assertEqual(review_verdict_label(R("minor"), ["oops"]), DISCUSS)


if __name__ == "__main__":
    unittest.main()
