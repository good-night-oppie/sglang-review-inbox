"""App boundary and ambiguous-write ledger regressions. No real GitHub writes."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inbox import config, github  # noqa: E402
from inbox.db import Store  # noqa: E402
from inbox.pipeline import Pipeline  # noqa: E402

HEAD = "a" * 40
REPO = "good-night-oppie/MontClip"
MARKER = "<!-- reviewinbox:" + "1" * 32 + " -->"


def review(marker=MARKER, **changes):
    return dict(id=17, html_url=f"https://github.com/{REPO}/pull/1#pullrequestreview-17",
                user={"login": "reviewinboxoppiexyz[bot]", "type": "Bot"},
                commit_id=HEAD, state="COMMENTED", body="fixture\n" + marker, **changes)


class ReviewAppBoundaryTest(unittest.TestCase):
    def setUp(self):
        # Import inside setUp makes baseline red failures report the missing seam.
        from inbox import review_app
        self.app = review_app

    def receipt(self, operation, result):
        return {"ok": True, "receipt": {"account": "reviewinbox", "app_slug": "reviewinboxoppiexyz",
                "installation_id": 180305629, "repo": REPO, "operation": operation,
                "execution": "completed"}, "result": result}

    def identity(self):
        return {"app_id": 5687106, "app_slug": "reviewinboxoppiexyz", "owner": "good-night-oppie",
                "permissions": {"contents": "read", "metadata": "read", "issues": "write", "pull_requests": "write"},
                "repositories": [{"full_name": REPO}, {"full_name": "good-night-oppie/MontClip-Cindy"}]}

    def test_installed_entry_is_resolved_and_hash_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-request.cjs"
            path.write_text("changed bundle", encoding="utf-8")
            with mock.patch.object(self.app, "ENTRY", path), mock.patch.object(self.app, "REAL_ENTRY", path):
                with self.assertRaises(self.app.AppError):
                    self.app.Client().request(REPO, "identity", {})

    def test_comment_payload_full_head_inline_and_no_shared_gh(self):
        calls = []
        def send(repo, op, args):
            calls.append((op, args))
            return self.identity() if op == "identity" else (
                {"head": {"sha": HEAD}, "state": "open", "draft": False} if op == "get_pull" else review())
        with mock.patch.object(self.app.Client, "request", side_effect=send), mock.patch.object(github, "gh") as gh:
            result = github.submit_agent_review(REPO, 1, HEAD, "APPROVE",
                [{"path": "x.py", "line": 1, "side": "RIGHT", "body": "REQUEST_CHANGES"}], marker=MARKER)
        self.assertEqual(result["id"], 17)
        op, payload = calls[-1]
        self.assertEqual(op, "submit_review")
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertIn(MARKER, payload["body"])
        self.assertIn("ReviewInboxOppieXYZ", payload["comments"][0]["body"])
        gh.assert_not_called()

    def test_wrong_identity_or_scope_never_submits(self):
        variants = [dict(self.identity(), app_id=1), dict(self.identity(), repositories=[]),
                    dict(self.identity(), permissions={"administration": "write"})]
        for identity in variants:
            with self.subTest(identity=identity), mock.patch.object(self.app.Client, "request", return_value=identity) as req:
                with self.assertRaises(self.app.AppError):
                    self.app.Client().submit(REPO, 1, HEAD, "body", [], MARKER)
                self.assertEqual(req.call_count, 1)

    def test_out_of_scope_or_short_head_is_rejected_locally(self):
        for repo, head in [("other/repo", HEAD), (REPO, "a" * 8)]:
            with mock.patch.object(self.app.Client, "request") as req:
                with self.assertRaises(self.app.AppError):
                    self.app.Client().submit(repo, 1, head, "body", [], MARKER)
                req.assert_not_called()

    def test_live_stale_or_closed_head_is_not_submitted(self):
        for snap in [{"head": {"sha": "b" * 40}, "state": "open"},
                     {"head": {"sha": HEAD}, "state": "closed"}]:
            with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap]) as req:
                with self.assertRaises(self.app.AppError):
                    self.app.Client().submit(REPO, 1, HEAD, "body", [], MARKER)
                self.assertEqual(req.call_count, 2)

    def test_process_timeout_invalid_response_and_unknown_are_uncertain_for_submit(self):
        bad = [subprocess.TimeoutExpired("node", 120), OSError("pipe lost"),
               subprocess.CompletedProcess([], 1, "not json", "secret stderr"),
               subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "execution": "unknown",
                                                              "errorCode": "WRITE_OUTCOME_UNKNOWN"}), "")]
        for outcome in bad:
            kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
            with self.subTest(outcome=type(outcome)), mock.patch.object(self.app.Client, "_entry", return_value=Path("bundle.cjs")), \
                    mock.patch.object(self.app.subprocess, "run", **kwargs):
                with self.assertRaises(self.app.AppError) as ctx:
                    self.app.Client().request(REPO, "submit_review", {"event": "COMMENT"})
                self.assertTrue(ctx.exception.unknown)
                self.assertNotIn("secret stderr", str(ctx.exception))

    def test_explicit_not_executed_is_distinct(self):
        proc = subprocess.CompletedProcess([], 1, json.dumps({"ok": False, "execution": "not_executed",
                                                            "errorCode": "STALE_HEAD"}), "")
        with mock.patch.object(self.app.Client, "_entry", return_value=Path("bundle.cjs")), \
                mock.patch.object(self.app.subprocess, "run", return_value=proc):
            with self.assertRaises(self.app.AppError) as ctx:
                self.app.Client().request(REPO, "submit_review", {"event": "COMMENT"})
            self.assertFalse(ctx.exception.unknown)

    def test_wrong_success_receipt_is_unknown_for_submit(self):
        receipt = self.receipt("submit_review", review())
        receipt["receipt"]["installation_id"] = 1
        with mock.patch.object(self.app.Client, "_entry", return_value=Path("bundle.cjs")), \
                mock.patch.object(self.app.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(receipt), "")):
            with self.assertRaises(self.app.AppError) as ctx:
                self.app.Client().request(REPO, "submit_review", {"event": "COMMENT"})
            self.assertTrue(ctx.exception.unknown)

    def test_submit_success_with_wrong_actor_is_still_unknown(self):
        item = review()
        item["user"] = {"login": "GoodNightOppie", "type": "User"}
        snap = {"head": {"sha": HEAD}, "state": "open"}
        with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap, item]):
            with self.assertRaises(self.app.AppError) as ctx:
                self.app.Client().submit(REPO, 1, HEAD, "body", [], MARKER)
            self.assertTrue(ctx.exception.unknown)

    def test_reconcile_paging_actor_head_marker_and_state(self):
        decoy = review()
        decoy["user"] = {"login": "QQOppieXYZ[bot]", "type": "Bot"}
        snap = {"head": {"sha": HEAD}, "state": "open"}
        with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap, [decoy] * 100, [review()]]) as req:
            self.assertEqual(self.app.Client().reconcile(REPO, 1, HEAD, MARKER)["id"], 17)
            self.assertEqual(req.call_args.args[2]["page"], 2)
        for field, value in [("commit_id", "b" * 40), ("state", "APPROVED"), ("body", "other"),
                             ("user", {"login": "reviewinboxoppiexyz[bot]", "type": "User"})]:
            item = review()
            item[field] = value
            with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap, [item]]):
                with self.assertRaises(self.app.AppError) as ctx:
                    self.app.Client().reconcile(REPO, 1, HEAD, MARKER)
                self.assertTrue(ctx.exception.unknown)

    def test_reconcile_after_remote_head_moves_never_completes_current_review(self):
        snap = {"head": {"sha": "b" * 40}, "state": "open"}
        with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap]) as req:
            with self.assertRaises(self.app.AppError) as ctx:
                self.app.Client().reconcile(REPO, 1, HEAD, MARKER)
            self.assertTrue(ctx.exception.unknown)
            self.assertEqual(req.call_count, 2)
            self.assertEqual(req.call_args.args[1], "get_pull")

    def test_explicitly_requested_draft_not_rejected_by_python_transport(self):
        snap = {"head": {"sha": HEAD}, "state": "open", "draft": True}
        with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap, review()]) as req:
            self.app.Client().submit(REPO, 1, HEAD, "body", [], MARKER)
            self.assertEqual(req.call_args.args[1], "submit_review")


class AppLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "inbox.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.conn.close)
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg.update(data_dir=self.tmp.name, mode="live", github_login="GoodNightOppie", notify_macos=False)
        cfg["agent"]["review_skill"]["repo"] = ""
        self.pipe = Pipeline(cfg, self.store)
        self.addCleanup(self.pipe.executor.shutdown)
        self.addCleanup(self.pipe.translator.shutdown)
        self.key = REPO + "#1"
        self.store.ensure_pr(REPO, 1)
        self.store.update_pr(self.key, head_sha=HEAD, review_head=HEAD, tier="T1",
                             review={"summary": "fixture", "findings": []})
        self.snap = mock.patch.object(github, "fetch_pr", return_value={"headRefOid": HEAD})
        self.snap.start()
        self.addCleanup(self.snap.stop)

    def test_unknown_then_remote_exists_no_duplicate_even_after_reopen(self):
        unknown = github.GitHubWriteUnknown("ambiguous")
        with mock.patch.object(github, "submit_agent_review", side_effect=unknown) as submit:
            with self.assertRaises(github.GitHubWriteUnknown):
                self.pipe.publish_agent_review(self.key)
            action = self.store.actions(self.key)[0]
            self.assertEqual(action["phase"], "unresolved")
            marker = action["result"]["marker"]
            reopened = Store(self.path)
            try:
                self.assertFalse(reopened.claim_action(self.key, HEAD, "review:agent-comment"))
            finally:
                reopened.conn.close()
            result = {"id": 17, "url": review()["html_url"], "marker": marker}
            with mock.patch.object(github, "reconcile_agent_review", return_value=result) as read:
                self.pipe.publish_agent_review(self.key)
                self.pipe.publish_agent_review(self.key)
                read.assert_called_once_with(REPO, 1, HEAD, marker)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(self.store.actions(self.key)[0]["phase"], "done")

    def test_failed_readback_and_repeat_remain_unresolved(self):
        with mock.patch.object(github, "submit_agent_review", side_effect=github.GitHubWriteUnknown("ambiguous")) as submit:
            with self.assertRaises(github.GitHubWriteUnknown):
                self.pipe.publish_agent_review(self.key)
            with mock.patch.object(github, "reconcile_agent_review", side_effect=github.GitHubError("read failed")):
                for _ in range(2):
                    with self.assertRaises(github.GitHubWriteUnknown):
                        self.pipe.publish_agent_review(self.key)
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(self.store.actions(self.key)[0]["phase"], "unresolved")

    def test_late_unknown_response_preserves_concurrently_reconciled_done_result(self):
        result = {"id": 17, "url": review()["html_url"]}
        def late_timeout(*args, **kwargs):
            self.store.finish_action(self.key, HEAD, "review:agent-comment", True, result)
            raise github.GitHubWriteUnknown("late response")
        with mock.patch.object(github, "submit_agent_review", side_effect=late_timeout):
            self.assertEqual(self.pipe.publish_agent_review(self.key), result)
        self.assertEqual(self.store.actions(self.key)[0]["phase"], "done")

    def test_legacy_running_without_marker_does_not_guess_or_repost(self):
        self.store.claim_action(self.key, HEAD, "review:agent-comment")
        with mock.patch.object(github, "submit_agent_review") as submit, \
                mock.patch.object(github, "reconcile_agent_review") as reconcile:
            with self.assertRaises(github.GitHubWriteUnknown):
                self.pipe.publish_agent_review(self.key)
            submit.assert_not_called()
            reconcile.assert_not_called()
        self.assertEqual(self.store.actions(self.key)[0]["phase"], "unresolved")

    def test_known_failure_releases_only_its_action(self):
        with mock.patch.object(github, "submit_agent_review", side_effect=github.GitHubError("not executed")):
            with self.assertRaises(github.GitHubError):
                self.pipe.publish_agent_review(self.key)
        self.assertEqual(self.store.actions(self.key), [])

    def test_done_action_cannot_be_downgraded_by_late_uncertain_or_failed_writer(self):
        event = "review:agent-comment"
        self.store.claim_action(self.key, HEAD, event, {"marker": MARKER})
        self.store.finish_action(self.key, HEAD, event, True, {"id": 17})
        self.store.unresolved_action(self.key, HEAD, event)
        self.store.finish_action(self.key, HEAD, event, False)
        self.assertEqual(self.store.actions(self.key)[0]["phase"], "done")

    def test_human_findings_use_app_without_widening_automatic_filter(self):
        f = {"title": "nit", "severity": "nit", "verify": "unverified", "kind": "evidence",
             "path": "x.py", "line": 1, "anchored": True, "comment": "look", "scenario": "fixture"}
        self.store.update_pr(self.key, review={"findings": [f]})
        with mock.patch.object(github, "submit_review") as human, \
                mock.patch.object(github, "submit_agent_review", return_value={"id": 17, "url": "u"}) as app:
            with self.assertRaises(ValueError):
                self.pipe.publish_findings(self.key, by="agent")
            self.pipe.publish_findings(self.key, by="human")
            app.assert_called_once()
            human.assert_not_called()
            self.assertNotIn("GoodNightOppie", app.call_args.args[3])
            self.assertNotIn("GoodNightOppie", app.call_args.args[4][0]["body"])

    def test_findings_unknown_readback_failure_keeps_claim(self):
        f = {"title": "bug", "severity": "major", "verify": "confirmed", "kind": "bug",
             "path": "x.py", "line": 1, "anchored": True, "comment": "look", "scenario": "fixture"}
        self.store.update_pr(self.key, review={"findings": [f]})
        with mock.patch.object(github, "submit_agent_review", side_effect=github.GitHubWriteUnknown("ambiguous")) as submit:
            with self.assertRaises(github.GitHubWriteUnknown):
                self.pipe.publish_findings(self.key, by="human")
            with mock.patch.object(github, "reconcile_agent_review", side_effect=github.GitHubError("read failed")):
                with self.assertRaises(github.GitHubWriteUnknown):
                    self.pipe.publish_findings(self.key, by="human")
            self.assertEqual(submit.call_count, 1)
            self.assertEqual(self.store.actions(self.key)[0]["phase"], "unresolved")


if __name__ == "__main__":
    unittest.main()
