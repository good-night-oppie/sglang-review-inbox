"""Six installed repositories must never widen the two-repository publisher."""
import copy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inbox import config, review_app  # noqa: E402

REPO = "good-night-oppie/MontClip"
MATRIX = {
    "good-night-oppie/MontClip": 1441621868,
    "good-night-oppie/MontClip-Cindy": 1446532339,
    "good-night-oppie/montclip-rsi": 1450602468,
    "good-night-oppie/rsi-playbooks": 1450603260,
    "good-night-oppie/oppie-github": 1451885379,
    "good-night-oppie/sglang-review-inbox": 1451900929,
}


def identity():
    return {"app_id": 5687106, "app_slug": "reviewinboxoppiexyz", "owner": "good-night-oppie",
            "permissions": {"contents": "read", "metadata": "read", "issues": "write", "pull_requests": "write"},
            "installationRepositories": [{"full_name": name, "id": ident} for name, ident in MATRIX.items()],
            "repositories": [{"full_name": name, "id": ident} for name, ident in list(MATRIX.items())[:2]]}


class InstallationScopeTest(unittest.TestCase):
    def test_paired_candidate_bundle_pin_is_consistent(self):
        expected = "4c306c3d94341ff2e8d43ab0212e7cf996d6faf0cff54814c57c22e4c6838813"
        self.assertEqual(review_app.SHA256, expected)
        self.assertEqual(config.DEFAULTS["review_app"]["sha256"], expected)

    def assert_refused(self, result):
        with mock.patch.object(review_app.Client, "request", return_value=result):
            with self.assertRaises(review_app.AppError):
                review_app.Client().identity(REPO)

    def test_exact_six_installation_and_two_execution_ids_are_accepted(self):
        value = identity()
        value["installationRepositories"].reverse()
        value["repositories"].reverse()
        with mock.patch.object(review_app.Client, "request", return_value=value):
            review_app.Client().identity(REPO)

    def test_missing_legacy_or_wrong_installation_scope_is_rejected(self):
        missing = identity()
        del missing["installationRepositories"]
        variants = [missing]
        for repos in (None, [], identity()["installationRepositories"][:2],
                      identity()["installationRepositories"] + [{"full_name": "good-night-oppie/extra", "id": 7}]):
            variants.append(dict(identity(), installationRepositories=repos))
        for value in variants:
            with self.subTest(value=value):
                self.assert_refused(value)

    def test_wrong_id_name_duplicates_and_malformed_repository_are_rejected(self):
        for field in ("installationRepositories", "repositories"):
            for mutation in ("wrong_id", "string_id", "wrong_owner", "duplicate", "malformed"):
                value = identity()
                if mutation == "wrong_id":
                    value[field][0]["id"] = 1
                elif mutation == "string_id":
                    value[field][0]["id"] = str(value[field][0]["id"])
                elif mutation == "wrong_owner":
                    value[field][0]["full_name"] = "other/MontClip"
                elif mutation == "duplicate":
                    value[field][0] = copy.deepcopy(value[field][1])
                else:
                    value[field][0]["full_name"] = []
                with self.subTest(field=field, mutation=mutation):
                    self.assert_refused(value)

    def test_six_repo_execution_token_is_refused(self):
        self.assert_refused(dict(identity(), repositories=identity()["installationRepositories"]))

    def test_scope_change_does_not_relax_permissions_or_app_identity(self):
        for field, replacement in (("app_id", 1), ("app_slug", "qqoppiexyz"), ("owner", "other")):
            self.assert_refused(dict(identity(), **{field: replacement}))
        for permissions in ({"contents": "write"}, dict(identity()["permissions"], contents="write"),
                            dict(identity()["permissions"], administration="write")):
            self.assert_refused(dict(identity(), permissions=permissions))

    def test_installed_only_repositories_are_refused_before_process_launch(self):
        # Neither a broader config claim nor the installation list is execution authorization.
        client = review_app.Client({"repositories": list(MATRIX)})
        with mock.patch.object(client, "_entry") as entry:
            for repo in list(MATRIX)[2:]:
                with self.subTest(repo=repo), self.assertRaises(review_app.AppError):
                    client.request(repo, "get_pull", {"number": 1})
            entry.assert_not_called()

    def test_approve_stays_refused_before_process_launch(self):
        with mock.patch.object(review_app.Client, "_entry") as entry:
            with self.assertRaises(review_app.AppError):
                review_app.Client().request(REPO, "submit_review", {"event": "APPROVE"})
            entry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
