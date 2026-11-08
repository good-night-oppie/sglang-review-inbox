import tempfile
import unittest
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inbox import github  # noqa: E402
from inbox.db import Store, pr_key  # noqa: E402
from inbox.pipeline import Pipeline  # noqa: E402

REPO = "good-night-oppie/MontClip-Cindy"
OTHER = "good-night-oppie/MontClip"
LOGIN = "GoodNightOppie"


def open_pr(number=46, *, body="ordinary body", draft=False):
    return {
        "number": number,
        "title": "review reconciliation fixture",
        "body": body,
        "isDraft": draft,
        "url": f"https://github.com/{REPO}/pull/{number}",
        "author": {"login": "EdwardTang"},
    }


class OpenPRReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / "fixture.sqlite3")
        self.addCleanup(self.store.conn.close)
        self.pipeline = Pipeline.__new__(Pipeline)
        self.pipeline.store = self.store
        self.pipeline.repos = {REPO, OTHER}
        self.pipeline.login = LOGIN

    def test_request_cleared_thirteen_seconds_before_poll_is_backfilled(self):
        # The 08:56:05 request was cleared by a COMMENT at 09:04:39; the next
        # search at 09:04:52 sees zero current requests, but the timeline retains it.
        events = [
            {"event": "review_requested", "created_at": "2026-11-08T08:56:05Z",
             "requested_reviewer": {"login": LOGIN}},
            {"event": "reviewed", "state": "commented", "submitted_at": "2026-11-08T09:04:39Z"},
        ]
        with mock.patch.object(github, "list_open_prs", side_effect=lambda repo: [open_pr()] if repo == REPO else []), \
             mock.patch.object(github, "pr_timeline", return_value=events) as timeline:
            self.assertEqual(self.pipeline.reconcile_open_prs(), 1)
            self.assertEqual(self.pipeline.reconcile_open_prs(), 0)
            timeline.assert_called_once_with(REPO, 46)
        card = self.store.get_pr(pr_key(REPO, 46))
        self.assertIsNotNone(card)
        self.assertEqual([r["source"] for r in card["requesters"]], ["reconcile"])
        self.assertEqual(card["requesters"][0]["id"], f"reconcile:{REPO}#46")

    def test_first_line_marker_needs_no_timeline_and_draft_is_skipped(self):
        items = [
            open_pr(1, draft=True),
            open_pr(2, body="QQ 请审\n负责人：BB"),
            open_pr(3, body=" QQ 请审\n负责人：BB"),
        ]
        with mock.patch.object(github, "list_open_prs", side_effect=lambda repo: items if repo == REPO else []), \
             mock.patch.object(github, "pr_timeline", return_value=[]) as timeline:
            self.assertEqual(self.pipeline.reconcile_open_prs(), 1)
            timeline.assert_called_once_with(REPO, 3)
        self.assertIsNone(self.store.get_pr(pr_key(REPO, 1)))
        self.assertIsNotNone(self.store.get_pr(pr_key(REPO, 2)))
        self.assertIsNone(self.store.get_pr(pr_key(REPO, 3)))

    def test_existing_card_is_not_duplicated_or_reopened(self):
        key = pr_key(REPO, 46)
        self.store.ensure_pr(REPO, 46, requester={"id": "manual:46", "source": "manual"})
        before = self.store.get_pr(key)["requesters"]
        with mock.patch.object(github, "list_open_prs", side_effect=lambda repo: [open_pr(body="QQ 请审")] if repo == REPO else []), \
             mock.patch.object(github, "pr_timeline") as timeline:
            self.assertEqual(self.pipeline.reconcile_open_prs(), 0)
            timeline.assert_not_called()
        self.assertEqual(self.store.get_pr(key)["requesters"], before)

    def test_unrelated_reviewer_does_not_create_a_card(self):
        events = [{"event": "review_requested", "requested_reviewer": {"login": "someone-else"}}]
        with mock.patch.object(github, "list_open_prs", side_effect=lambda repo: [open_pr()] if repo == REPO else []), \
             mock.patch.object(github, "pr_timeline", return_value=events):
            self.assertEqual(self.pipeline.reconcile_open_prs(), 0)
        self.assertIsNone(self.store.get_pr(pr_key(REPO, 46)))

    def test_list_and_timeline_failures_are_not_swallowed(self):
        with mock.patch.object(github, "list_open_prs", side_effect=github.GitHubError("404 repository read")):
            with self.assertRaises(github.GitHubError):
                self.pipeline.reconcile_open_prs()
        with mock.patch.object(github, "list_open_prs", side_effect=lambda repo: [open_pr()] if repo == REPO else []), \
             mock.patch.object(github, "pr_timeline", side_effect=github.GitHubError("404 timeline")):
            with self.assertRaises(github.GitHubError):
                self.pipeline.reconcile_open_prs()
        self.assertIsNone(self.store.get_pr(pr_key(REPO, 46)))

    def test_review_request_poll_combines_search_and_reconcile(self):
        with mock.patch.object(self.pipeline, "_github_search", return_value=0), \
             mock.patch.object(self.pipeline, "reconcile_open_prs", return_value=1) as reconcile:
            self.assertEqual(self.pipeline.poll_review_requested({}), 1)
            reconcile.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
