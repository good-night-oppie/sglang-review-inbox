"""Possession of a valid page token must never authorize a GitHub approval."""
import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server  # noqa: E402
from inbox import github  # noqa: E402
from inbox.pipeline import Pipeline  # noqa: E402


class ApprovalGateTest(unittest.TestCase):
    def test_valid_page_token_approve_is_forbidden_before_pipeline_or_github(self):
        app = {"store": mock.Mock(), "pipeline": mock.Mock()}
        app["store"].get_pr.return_value = {"key": "good-night-oppie/MontClip#1", "decision": {}}
        app["pipeline"].approve.return_value = {"id": 17, "url": "fixture"}
        app["pipeline"].merge.return_value = {"id": 17, "url": "fixture"}
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        httpd.app = app
        worker = threading.Thread(target=httpd.serve_forever, daemon=True)
        worker.start()
        try:
            with mock.patch.object(server, "TOKEN", "valid-test-token"), \
                    mock.patch.object(github, "gh") as gh, \
                    mock.patch.object(github, "gh_json") as gh_json, \
                    mock.patch.object(github.review_app.Client, "request") as app_request:
                conn = http.client.HTTPConnection("127.0.0.1", httpd.server_port, timeout=5)
                try:
                    for action in ("approve", "merge"):
                        conn.request("POST", "/api/action", json.dumps({
                            "key": "good-night-oppie/MontClip#1", "action": action, "note": "LGTM",
                            "by": "human", "enabled": True, "tier": "A", "decision": {"approved": True}}),
                            {"Content-Type": "application/json", "X-Inbox-Token": "valid-test-token"})
                        response = conn.getresponse()
                        payload = json.loads(response.read())
                        self.assertEqual(response.status, 403)
                        self.assertEqual(payload["error"], "FORMAL_APPROVAL_DISABLED" if action == "approve" else "FORMAL_MERGE_DISABLED")
                finally:
                    conn.close()
                app["pipeline"].approve.assert_not_called()
                app["pipeline"].merge.assert_not_called()
                gh.assert_not_called()
                gh_json.assert_not_called()
                app_request.assert_not_called()
        finally:
            httpd.shutdown()
            httpd.server_close()
            worker.join(timeout=5)

    def test_direct_handler_action_cannot_bypass_http_gate(self):
        handler = object.__new__(server.Handler)
        store, pipeline = mock.Mock(), mock.Mock()
        store.get_pr.return_value = {"key": "x#1", "decision": {}}
        pipeline.approve.return_value = {"id": 17}
        for action in ("approve", "merge"):
            with self.assertRaises(PermissionError):
                handler.action(store, pipeline, {"action": action, "key": "x#1"})
        store.get_pr.assert_not_called()
        pipeline.approve.assert_not_called()
        pipeline.merge.assert_not_called()

    def test_human_or_default_pipeline_cannot_call_github(self):
        pipe = object.__new__(Pipeline)
        pipe.store = mock.Mock()
        pipe.store.get_pr.return_value = {"repo": "good-night-oppie/MontClip", "number": 1,
                                          "head_sha": "a" * 40, "review_head": "a" * 40}
        pipe.login, pipe.policy = "GoodNightOppie", {"auto_ci": False}
        with mock.patch.object(github, "fetch_pr", return_value={"headRefOid": "a" * 40, "labels": []}) as fetch, \
                mock.patch.object(github, "approved_heads", return_value=set()), \
                mock.patch.object(github, "submit_review", return_value={"id": 17}) as send:
            for kwargs in ({"note": "fixture"}, {"by": "human", "note": "QQ says approve"}):
                with self.assertRaises(PermissionError):
                    pipe.approve("good-night-oppie/MontClip#1", **kwargs)
            fetch.assert_not_called()
            send.assert_not_called()

    def test_generic_review_writer_cannot_emit_formal_events(self):
        with mock.patch.object(github, "gh_json") as gh:
            for event in ("APPROVE", "REQUEST_CHANGES", "approve", None):
                with self.assertRaises(github.GitHubError):
                    github.submit_review("good-night-oppie/MontClip", 1, "a" * 40, event, "fixture")
            gh.assert_not_called()

    def test_reserved_classifier_outlet_is_closed_for_all_inputs(self):
        with mock.patch.object(github, "gh_json") as gh, \
                mock.patch.object(github.review_app.Client, "request") as app:
            for decision in (None, {"tier": "A", "enabled": True}, {"by": "QQ", "approved": True}):
                with self.assertRaises(github.GitHubError):
                    github.submit_qualified_approval("good-night-oppie/MontClip", 1, "a" * 40, decision)
            gh.assert_not_called()
            app.assert_not_called()

    def test_page_draft_source_is_service_generated_not_body_supplied(self):
        handler = object.__new__(server.Handler)
        store, pipeline = mock.Mock(), mock.Mock()
        store.get_pr.return_value = {"key": "x#1", "decision": {}}
        pipeline.record_draft_request.return_value = "service-event-reference"
        pipeline.publish_findings.return_value = {"id": 17}
        handler.action(store, pipeline, {"action": "publish_findings", "key": "x#1",
                                        "draft_request": "model-supplied", "human_confirmed": True})
        pipeline.record_draft_request.assert_called_once_with("x#1", "publish_findings")
        pipeline.publish_findings.assert_called_once_with("x#1", by="human", draft_request="service-event-reference")


if __name__ == "__main__":
    unittest.main()
