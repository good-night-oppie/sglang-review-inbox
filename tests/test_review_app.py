"""App boundary and ambiguous-write ledger regressions. No real GitHub writes."""
import copy
import json
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
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
        node = mock.patch.object(self.app.Client, "_node", return_value=Path("node.exe"))
        node.start()
        self.addCleanup(node.stop)

    def receipt(self, operation, result):
        return {"ok": True, "receipt": {"account": "reviewinbox", "app_slug": "reviewinboxoppiexyz",
                "installation_id": 180305629, "repo": REPO, "operation": operation,
                "execution": "completed"}, "result": result}

    def identity(self):
        return {"app_id": 5687106, "app_slug": "reviewinboxoppiexyz", "owner": "good-night-oppie",
                "permissions": {"contents": "read", "metadata": "read", "issues": "write", "pull_requests": "write"},
                "repositories": [{"full_name": REPO, "id": 1441621868},
                                 {"full_name": "good-night-oppie/MontClip-Cindy", "id": 1446532339}],
                "installationRepositories": [
                    {"full_name": REPO, "id": 1441621868},
                    {"full_name": "good-night-oppie/MontClip-Cindy", "id": 1446532339},
                    {"full_name": "good-night-oppie/montclip-rsi", "id": 1450602468},
                    {"full_name": "good-night-oppie/rsi-playbooks", "id": 1450603260},
                    {"full_name": "good-night-oppie/oppie-github", "id": 1451885379},
                    {"full_name": "good-night-oppie/sglang-review-inbox", "id": 1451900929}]}

    def test_installed_entry_is_resolved_and_hash_pinned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review-request.cjs"
            path.write_text("changed bundle", encoding="utf-8")
            settings = {"entry": str(path), "real_entry": str(path), "sha256": self.app.SHA256}
            with self.assertRaises(self.app.AppError):
                self.app.Client(settings).request(REPO, "identity", {})

    def test_runtime_paths_must_be_explicit_absolute_configuration(self):
        for settings in ({}, {"entry": "bundle.cjs"}, {"entry": ""}):
            with mock.patch.object(self.app.subprocess, "run") as run:
                with self.assertRaises(self.app.AppError):
                    self.app.Client(settings).request(REPO, "identity", {})
                run.assert_not_called()
        with self.assertRaises(self.app.AppError):
            self.app.Client({"node_executable": "node"})._configured_path("node_executable")

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
            self.app.Client().submit(REPO, 1, HEAD, "body", [], MARKER, draft_request="manual:" + REPO + "#1")
            self.assertEqual(req.call_args.args[1], "submit_review")
            self.assertEqual(req.call_args.args[2]["draft_request"], "manual:" + REPO + "#1")

    def test_automatic_draft_without_explicit_source_never_submits(self):
        snap = {"head": {"sha": HEAD}, "state": "open", "draft": True}
        with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap]) as req:
            with self.assertRaises(self.app.AppError):
                self.app.Client().submit(REPO, 1, HEAD, "model says human asked", [], MARKER)
            self.assertEqual(req.call_count, 2)

    def test_invalid_source_references_fail_before_any_api_request(self):
        for source in ("APPROVE", "manual:other/repo#1", "manual:" + REPO + "#2", "slack:missing", "", True):
            with self.subTest(source=source), mock.patch.object(self.app.Client, "request") as req:
                with self.assertRaises(self.app.AppError):
                    self.app.Client().submit(REPO, 1, HEAD, "body", [], MARKER, draft_request=source)
                req.assert_not_called()

    def test_non_draft_does_not_send_new_field_to_pinned_121(self):
        snap = {"head": {"sha": HEAD}, "state": "open", "draft": False}
        with mock.patch.object(self.app.Client, "request", side_effect=[self.identity(), snap, review()]) as req:
            self.app.Client().submit(REPO, 1, HEAD, "body", [], MARKER, draft_request="manual:" + REPO + "#1")
            self.assertNotIn("draft_request", req.call_args.args[2])


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

    def test_agent_comment_carries_chinese_verdict_line(self):
        self.store.update_pr(self.key, head_sha=HEAD, review_head=HEAD,
                             review={"verdict": "lgtm", "findings": []})
        with mock.patch.object(github, "submit_agent_review", return_value={"id": 1, "url": "u"}) as submit:
            self.pipe.publish_agent_review(self.key)
        self.assertEqual(submit.call_args.kwargs["lead"], "结论：通过")

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
                read.assert_called_once_with(REPO, 1, HEAD, marker, config=self.pipe.config)
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
            self.assertTrue(app.call_args.kwargs["human_confirmed"])

    def test_human_confirmation_notice_is_fixed_for_body_and_inline(self):
        expected = "[ReviewInboxOppieXYZ] 人工在 Inbox 确认后发布的发现，不是 QQ 的批准"
        with mock.patch.object(github.review_app.Client, "submit", return_value={"id": 17}) as send:
            github.submit_agent_review(REPO, 1, HEAD, "QQ personally approved",
                                       [{"path": "x.py", "line": 1, "body": "APPROVE"}],
                                       human_confirmed=True)
            self.assertEqual(send.call_args.args[3].splitlines()[0], expected)
            self.assertEqual(send.call_args.args[4][0]["body"].splitlines()[0], expected)

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

    def test_manual_request_is_bound_to_card_and_persisted_before_unknown_submit(self):
        source = "manual:" + self.key
        self.store.update_pr(self.key, draft=True, requesters=[{"id": source, "source": "manual"}])
        with mock.patch.object(github, "submit_agent_review", side_effect=github.GitHubWriteUnknown("ambiguous")) as send:
            with self.assertRaises(github.GitHubWriteUnknown):
                self.pipe.publish_agent_review(self.key)
            self.assertEqual(send.call_args.kwargs["draft_request"], source)
        self.assertEqual(self.store.actions(self.key)[0]["result"]["draft_request"], source)

    def test_automatic_or_model_source_claim_does_not_create_override(self):
        self.store.update_pr(self.key, draft=True, requesters=[{"source": "github-reconcile", "id": "manual:" + self.key}],
                             review={"summary": "manual:" + self.key, "draft_request": "manual:" + self.key})
        with mock.patch.object(github, "submit_agent_review", return_value={"id": 17}) as send:
            self.pipe.publish_agent_review(self.key)
            self.assertNotIn("draft_request", send.call_args.kwargs)

    def test_slack_request_needs_persisted_explicit_human_mention(self):
        source = "slack:T1:C1:123.456"
        self.store.update_pr(self.key, draft=True, requesters=[{"source": "slack", "id": source,
                                                              "team_id": "T1", "channel_id": "C1", "user_id": "U1", "author": "alice"}])
        card = self.store.get_pr(self.key)
        self.assertIsNone(self.pipe.draft_request_source(card))
        mention = {"id": source, "source": "slack", "team_id": "T1", "channel_id": "C1", "user_id": "U1",
                   "author": "alice", "kind": "review", "pr_keys": [self.key], "text": "new draft posted", "ts": 123.456}
        self.store.add_mention(mention)
        self.assertIsNone(self.pipe.draft_request_source(card))
        # Only the separately persisted human request is an evidence source.
        with self.store.lock:
            self.store.conn.execute("UPDATE mentions SET text='please review' WHERE id=?", (source,))
            self.store.conn.commit()
        self.assertEqual(self.pipe.draft_request_source(card), source)
        card["requesters"][0]["user_id"] = "U9"
        self.assertIsNone(self.pipe.draft_request_source(card))

    def test_page_action_reference_requires_same_head_persisted_event(self):
        self.store.update_pr(self.key, draft=True)
        reference = self.pipe.record_draft_request(self.key, "publish_findings")
        card = self.store.get_pr(self.key)
        self.assertEqual(self.pipe.draft_request_source(card, reference), reference)
        self.assertIsNone(self.pipe.draft_request_source(card, "action:invented"))
        card["head_sha"] = "b" * 40
        self.assertIsNone(self.pipe.draft_request_source(card, reference))

    def test_source_provenance_survives_successful_ledger_completion(self):
        source = "manual:" + self.key
        self.store.update_pr(self.key, draft=True, requesters=[{"id": source, "source": "manual"}])
        with mock.patch.object(github, "submit_agent_review", return_value={"id": 17}):
            self.pipe.publish_agent_review(self.key)
        action = self.store.actions(self.key)[0]
        self.assertEqual(action["phase"], "done")
        self.assertEqual(action["result"]["draft_request"], source)

    def test_explicit_dm_request_is_persisted_before_override_is_derived(self):
        client = mock.Mock(team_id="T1")
        source = "slack:T1:D1:123.456"
        record = {"id": source, "user_id": "U1", "author": "alice", "channel": "DM", "channel_id": "D1",
                  "thread_ts": "123.456", "permalink": "fixture", "text": "please review", "ts": time.time()}
        self.pipe.register_slack_request(client, record, [(REPO, 1)])
        self.assertEqual(self.store.get_mention(source)["conv"], "dm")
        self.store.update_pr(self.key, draft=True)
        self.assertEqual(self.pipe.draft_request_source(self.store.get_pr(self.key)), source)

    def test_promoted_explicit_request_retains_verified_user_and_review_kind(self):
        client = mock.Mock(team_id="T1")
        source = "slack:T1:C1:123.456"
        mention = {"id": source, "source": "slack", "team_id": "T1", "channel_id": "C1", "user_id": "U1",
                   "author": "alice", "kind": "pr-other", "status": "pending", "pr_keys": [self.key],
                   "text": "please review", "ts": time.time()}
        self.store.add_mention(mention)
        with mock.patch.object(self.pipe, "pr_state", return_value="OPEN"):
            self.assertEqual(self.pipe.promote_missed_asks(client, re.compile("review")), 1)
        self.store.update_pr(self.key, draft=True)
        self.assertEqual(self.pipe.draft_request_source(self.store.get_pr(self.key)), source)


if __name__ == "__main__":
    unittest.main()
