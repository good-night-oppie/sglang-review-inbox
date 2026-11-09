"""Automatic review payloads stay advisory even when the model asks to approve."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inbox import agent as github_agent, config, github  # noqa: E402
from inbox.db import Store  # noqa: E402
from inbox.pipeline import Pipeline  # noqa: E402

HEAD = "a" * 40


class AgentReviewPayloadTest(unittest.TestCase):
    def test_notice_is_the_required_literal_not_a_configurable_model_prompt(self):
        self.assertEqual(github.AGENT_REVIEW_NOTICE,
                         "[ReviewInboxOppieXYZ Review Agent] 自动审查，仅供参考，不是 QQ 的批准")

    def test_model_requests_cannot_choose_the_github_event(self):
        for verdict in ("approve", "APPROVE", "REQUEST_CHANGES", "lgtm", "changes_needed", "needs_human"):
            with self.subTest(verdict=verdict), mock.patch.object(github.review_app.Client, "submit", return_value={"id": 7}) as send:
                github.submit_agent_review("example/repo", 1, HEAD, verdict,
                                           [{"path": "fixture.py", "line": 1, "body": verdict}])
                args = send.call_args.args
                self.assertEqual(args[2], HEAD)
                for body in (args[3], args[4][0]["body"]):
                    self.assertEqual(body.splitlines()[0], github.AGENT_REVIEW_NOTICE)
                    self.assertIn(HEAD, body)
                self.assertTrue(args[5].startswith("<!-- reviewinbox:"))

    def test_missing_or_short_head_is_rejected_before_any_write(self):
        for head in ("", "a" * 8, "not-a-sha", None):
            with self.subTest(head=head), mock.patch.object(github, "gh_json") as send:
                with self.assertRaises(ValueError):
                    github.submit_agent_review("example/repo", 1, head, "approve")
                send.assert_not_called()


class AutomaticReviewPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = copy.deepcopy(config.DEFAULTS)
        self.cfg.update(mode="live", github_login="GoodNightOppie", notify_macos=False)
        self.cfg["agent"]["review_skill"]["repo"] = ""
        self.cfg["policy"].update(auto_approve=True, auto_publish_findings=True,
                                  auto_author_notes=False, auto_ci=False, auto_merge=False,
                                  auto_discuss=False, slack_ack=False, slack_report=False)
        self.store = Store(Path(self.tmp.name) / "fixture.sqlite3")
        self.addCleanup(self.store.conn.close)
        usage = mock.patch.object(github_agent, "usage_hook", None)
        usage.start()
        self.addCleanup(usage.stop)
        self.pipe = Pipeline(self.cfg, self.store)
        self.addCleanup(self.pipe.executor.shutdown)
        self.addCleanup(self.pipe.translator.shutdown)
        self.key = "example/repo#1"
        self.store.ensure_pr("example/repo", 1)
        self.review = {"verdict": "lgtm", "agent_verdict": "lgtm", "findings": [],
                       "approve_note": "通过。", "summary": "fixture", "head": HEAD}
        self.store.update_pr(self.key, head_sha=HEAD, review_head=HEAD, review=self.review, tier="T1")
        self.snap = {"number": 1, "title": "fixture", "author": {"login": "fixture"},
                     "url": "https://github.com/example/repo/pull/1", "state": "OPEN", "isDraft": False,
                     "headRefOid": HEAD, "baseRefName": "main", "additions": 1, "deletions": 0,
                     "changedFiles": 1, "files": [], "labels": [], "mergeable": "MERGEABLE",
                     "mergeStateStatus": "CLEAN", "reviewDecision": "REVIEW_REQUIRED",
                     "reviews": [], "latestReviews": [], "body": "fixture"}
        self.payloads = []

        def send(*args, **kwargs):
            self.payloads.append(json.loads(kwargs["input_text"]))
            return {"id": len(self.payloads), "html_url": "https://github.com/example/repo/pull/1#review"}

        def app_send(repo, number, head, body, comments, marker):
            self.payloads.append({"commit_id": head, "event": "COMMENT", "body": body, "comments": list(comments)})
            return {"id": len(self.payloads), "url": "https://github.com/example/repo/pull/1#review", "marker": marker}

        for patch in (
            mock.patch.object(github, "gh_json", side_effect=send),
            mock.patch.object(github.review_app.Client, "submit", side_effect=app_send),
            mock.patch.object(github, "fetch_pr", side_effect=lambda *args: self.snap),
            mock.patch.object(github, "approved_heads", return_value=set()),
            mock.patch("inbox.pipeline.triage", return_value={"tier": "T1", "gates": []}),
            mock.patch("inbox.pipeline.priority", return_value=0),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.pipe.answer_replies = mock.Mock(return_value=False)
        self.pipe.try_fix = mock.Mock(return_value=False)
        self.pipe.handle_ci = mock.Mock()

    def assert_advisory_only(self):
        self.assertTrue(self.payloads)
        self.assertEqual({p["event"] for p in self.payloads}, {"COMMENT"})
        self.assertTrue(all(p["commit_id"] == HEAD for p in self.payloads))
        self.assertTrue(all(p["body"].splitlines()[0] == github.AGENT_REVIEW_NOTICE for p in self.payloads))
        card = self.store.get_pr(self.key)
        self.assertNotEqual(card["stage"], "approved")
        self.assertNotIn("approved_head", card["decision"])
        self.assertFalse(any(a["event"] in ("review:approve", "merge") for a in self.store.actions(self.key)))
        self.assertFalse(self.cfg["policy"]["auto_merge"])

    def test_clean_model_verdict_is_commented_and_waits_for_qq(self):
        self.pipe.advance(self.store.get_pr(self.key))
        self.assert_advisory_only()
        self.assertEqual(self.store.get_pr(self.key)["stage"], "ready")
        self.assertFalse(self.pipe.handle_ci.call_args.kwargs["merge_gate"])

    def test_model_interpreted_maintainer_lgtm_is_not_a_formal_approval(self):
        self.store.update_pr(self.key, decision={"approve_head": HEAD})
        self.pipe.advance(self.store.get_pr(self.key))
        self.assert_advisory_only()
        self.assertEqual(len(self.payloads), 1)
        self.assertFalse(self.pipe.handle_ci.call_args.kwargs["merge_gate"])

    def test_direct_automatic_approve_call_is_also_comment_only(self):
        self.pipe.approve(self.key, by="agent")
        self.assert_advisory_only()

    def test_unknown_automatic_actor_cannot_reach_formal_approval(self):
        self.pipe.approve(self.key, by="model")
        self.assert_advisory_only()

    def test_repeated_advance_does_not_post_duplicate_advisories(self):
        for _ in range(3):
            self.pipe.advance(self.store.get_pr(self.key))
        self.assert_advisory_only()
        self.assertEqual(len(self.payloads), 1)

    def test_stale_head_never_posts_a_comment_for_the_old_review(self):
        self.snap["headRefOid"] = "b" * 40
        with self.assertRaises(ValueError):
            self.pipe.approve(self.key, by="agent")
        self.assertEqual(self.payloads, [])

    def test_old_agent_approval_action_cannot_resurrect_a_dismissed_approval(self):
        self.store.claim_action(self.key, HEAD, "review:approve")
        self.store.finish_action(self.key, HEAD, "review:approve", True, {"by": "agent"})
        self.pipe.advance(self.store.get_pr(self.key))
        card = self.store.get_pr(self.key)
        self.assertEqual({p["event"] for p in self.payloads}, {"COMMENT"})
        self.assertEqual(card["stage"], "ready")
        self.assertNotIn("approved_head", card["decision"])
        self.assertFalse(card["decision"]["merge_ready"])
        self.assertEqual(len([a for a in self.store.actions(self.key) if a["event"] == "review:approve"]), 1)

    def test_old_approved_cache_is_cleared_when_github_has_no_current_approval(self):
        self.store.update_pr(self.key, decision={"approved_head": HEAD, "merge_ready": True})
        self.snap["reviews"] = [{"author": {"login": "GoodNightOppie"}, "state": "DISMISSED",
                                 "commit": {"oid": HEAD}, "submittedAt": "2026-11-07T19:49:00Z"}]
        self.pipe.advance(self.store.get_pr(self.key))
        self.assert_advisory_only()
        self.assertFalse(self.store.get_pr(self.key)["decision"]["merge_ready"])

    def test_needs_human_does_not_reintroduce_the_old_approved_cache(self):
        self.store.update_pr(self.key, decision={"approved_head": HEAD, "merge_ready": True},
                             review=dict(self.review, verdict="needs_human", agent_verdict="needs_human"))
        self.pipe.advance(self.store.get_pr(self.key))
        card = self.store.get_pr(self.key)
        self.assertEqual(card["stage"], "ready")
        self.assertNotIn("approved_head", card["decision"])
        self.assertFalse(card["decision"]["merge_ready"])

    def test_confirmed_findings_are_comments_including_inline_disclaimer(self):
        finding = {"title": "Fixture risk", "severity": "major", "kind": "bug", "category": "correctness",
                   "path": "fixture.py", "line": 1, "anchored": True, "verify": "confirmed",
                   "scenario": "fixture", "comment": "Could this fail?", "evidence": "fixture"}
        self.store.update_pr(self.key, review=dict(self.review, verdict="changes_needed",
                                                   agent_verdict="changes_needed", findings=[finding]))
        self.pipe.advance(self.store.get_pr(self.key))
        self.assert_advisory_only()
        self.assertEqual(self.payloads[0]["comments"][0]["body"].splitlines()[0], github.AGENT_REVIEW_NOTICE)

    def test_explicit_human_button_cannot_become_a_formal_approval(self):
        with self.assertRaises(PermissionError):
            self.pipe.approve(self.key, note="QQ explicit manual review of this head", by="human")
        self.assertEqual(self.payloads, [])


if __name__ == "__main__":
    unittest.main()
