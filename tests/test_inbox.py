import contextlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from inbox import agent, ci, config as config_module, fixer, github, pipeline as pipeline_module  # noqa: E402
from inbox.db import Store  # noqa: E402
from inbox.pipeline import Pipeline, anchored, evidence_line, finding_comment, new_side_ranges, plain  # noqa: E402
from inbox.skill import SkillSync, SkillSyncError, missing_skill_steps  # noqa: E402
from inbox.triage import triage  # noqa: E402

# Tests act as one fixed maintainer instead of asking `gh` who is logged in.
config_module.DEFAULTS["github_login"] = "BBuf"

POLICY = config_module.DEFAULTS["policy"]
HEAD = "a" * 40
NEW_HEAD = "b" * 40


def snapshot(head=HEAD, labels=(), rollup=(), files=None, body="Test: pytest test/registered/x/test_y.py passed",
             title="[Fix] scheduler: handle empty batch", state="OPEN", draft=False, mergeable="MERGEABLE"):
    files = files if files is not None else [
        {"path": "python/sglang/srt/managers/scheduler.py", "additions": 30, "deletions": 5}]
    return {
        "number": 7, "title": title, "author": {"login": "someone"}, "url": "https://github.com/sgl-project/sglang/pull/7",
        "state": state, "isDraft": draft, "headRefOid": head, "baseRefName": "main",
        "additions": sum(f["additions"] for f in files), "deletions": sum(f["deletions"] for f in files),
        "changedFiles": len(files), "files": files, "labels": [{"name": n} for n in labels],
        "mergeable": mergeable, "mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED",
        "body": body, "statusCheckRollup": list(rollup), "commits": [], "latestReviews": [],
        "createdAt": "", "updatedAt": "",
    }


def check(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "workflowName": "PR Test", "name": name, "status": status,
            "conclusion": conclusion, "detailsUrl": f"https://github.com/x/y/actions/runs/1/job/{abs(hash(name)) % 1000}"}


class TriageTest(unittest.TestCase):
    def test_hot_path_large_change_is_t3(self):
        files = [{"path": "scripts/clip.py", "additions": 500, "deletions": 20}]  # jj-agent: export engine is the hot path
        self.assertEqual(triage(snapshot(files=files), POLICY)["tier"], "T3")

    def test_docs_only_is_t1_without_test_plan_gate(self):
        files = [{"path": "docs/advanced.md", "additions": 10, "deletions": 2}]
        result = triage(snapshot(files=files, body="docs"), POLICY)
        self.assertEqual(result["tier"], "T1")
        self.assertEqual(result["gates"], [])

    def test_gates(self):
        result = triage(snapshot(draft=True, body="", title="[Perf] faster decode"), POLICY)
        reasons = " ".join(g["reason"] for g in result["gates"])
        self.assertIn("draft", reasons)
        self.assertIn("no test commands", reasons)
        self.assertIn("before/after", reasons)

    def test_montclip_npm_results_count_as_test_evidence(self):
        # MontClip PR bodies report npm results in Chinese; that is a test plan, not a missing one
        files = [{"path": "app.js", "additions": 40, "deletions": 3}]
        for body in ("Windows（本机，head 13687bc）：共 1550，通过 1545，失败 0，跳过 5",
                     "共 30, 通过 30, 失败 0", "跑了 npm test", "node --test test/a.test.js"):
            reasons = " ".join(g["reason"] for g in triage(snapshot(files=files, body=body), POLICY)["gates"])
            self.assertNotIn("no test commands", reasons, body)
        reasons = " ".join(g["reason"] for g in triage(snapshot(files=files, body="fix the export"), POLICY)["gates"])
        self.assertIn("no test commands", reasons)

    def test_montclip_test_dir_counts_as_touching_tests(self):
        files = [{"path": "app.js", "additions": 40, "deletions": 3},
                 {"path": "test/export-frame-zh.test.js", "additions": 33, "deletions": 0}]
        reasons = " ".join(g["reason"] for g in triage(snapshot(files=files, body="fix the export"), POLICY)["gates"])
        self.assertNotIn("no test commands", reasons)

    # ---- size gate: lines to review, lockfiles, QQ-recorded size exceptions ------------------------------------
    SZ_HEAD = "1fcbed24" + "0" * 32
    SZ_KEY = "good-night-oppie/MontClip#109"
    SZ_VEND = [{"path": "agent-ui/cindy-43001ea/a.js", "additions": 3000, "deletions": 0},
               {"path": "agent-ui/cindy-43001ea/b.js", "additions": 3000, "deletions": 50}]
    SZ_GEN = [{"path": "cindy-parts.css", "additions": 2991, "deletions": 0}]
    SZ_LOCK = [{"path": "agent-ui/package-lock.json", "additions": 1000, "deletions": 40}]
    SZ_HAND = [{"path": "agent-ui/styles.cjs", "additions": 120, "deletions": 8},
               {"path": "test/cindy-parts.test.js", "additions": 200, "deletions": 0}]

    @staticmethod
    def sz_total(files):
        return sum(f["additions"] + f["deletions"] for f in files)

    def sz_pr(self, files, head=None, number=109, drop_files=False, **extra):
        pr = snapshot(files=files, head=head or self.SZ_HEAD)
        pr["number"], pr["url"] = number, f"https://github.com/good-night-oppie/MontClip/pull/{number}"
        pr.update(extra)
        if drop_files:  # the per-file query failed: totals are known, the list is empty
            pr["files"] = []
        return pr

    def sz_exception(self, review=None, vendored=None, generated=None, key=None):
        paths = [f["path"] for f in self.SZ_VEND + self.SZ_GEN]
        rev = {"by": "QQ", "source": "QQ review of the current head (demo record)", "verdict": "accepted",
               "head": self.SZ_HEAD, "files": paths}
        vend = {"kind": "vendored", "files": [f["path"] for f in self.SZ_VEND],
                "upstream": {"repo": "good-night-oppie/cindy", "commit": "43001eaa" + "0" * 32},
                "licence": "MIT, checked by QQ", "verified": {"byte_identical": True, "method": "git blob sha1 + sha256"}}
        gen = {"kind": "generated", "files": [f["path"] for f in self.SZ_GEN],  # this repo's output: no upstream at all
               "generated": {"inputs": ["agent-ui/cindy-43001ea/globals.css"], "command": "node scripts/build-parts.js",
                             "result_sha256": {"cindy-parts.css": "a" * 64}, "reproduced": True,
                             "record": "QQ reproduction record (demo)"}}
        rev.update(review or {})
        vend.update(vendored or {})
        gen.update(generated or {})
        return {key or self.SZ_KEY: {"review": rev, "groups": [vend, gen]}}

    def sz_run(self, files, exceptions=None, **kwargs):
        policy = dict(POLICY, size_exceptions=exceptions or {}, size_exceptions_trusted_source=kwargs.pop("trusted", True))
        policy.update(kwargs.pop("policy", {}))
        result = triage(self.sz_pr(files, **kwargs), policy)
        return result, {g["id"]: g for g in result["gates"]}

    def test_lockfile_is_excluded_by_exact_name_only_and_never_hides_the_total(self):
        result, gates = self.sz_run(self.SZ_LOCK)
        self.assertNotIn("size", gates)
        self.assertEqual(result["lines"], 1040)  # the total additions+deletions is always kept
        self.assertEqual(result["excluded_files"], [{"path": "agent-ui/package-lock.json", "lines": 1040, "why": "lockfile"}])
        self.assertEqual(gates["provenance"]["kind"], "human")
        self.assertIn("agent-ui/package-lock.json (1040 lines, lockfile)", gates["provenance"]["reason"])
        self.assertIn("install scripts", gates["provenance"]["reason"])
        # names that merely look like lockfiles are counted
        look_alike = [{"path": "docs/package-lock.json.md", "additions": 2600, "deletions": 0},
                      {"path": "my-yarn.lock", "additions": 10, "deletions": 0}]
        result, gates = self.sz_run(look_alike)
        self.assertEqual(result["excluded_lines"], 0)
        self.assertIn("size", gates)
        # a lockfile cannot hide hand-written lines: the reason shows the total and what was not counted
        result, gates = self.sz_run(self.SZ_LOCK + [{"path": "scripts/clip.py", "additions": 2600, "deletions": 0}])
        self.assertIn("2600 changed lines", gates["size"]["reason"])
        self.assertIn("3640 total additions+deletions, 1040 not counted", gates["size"]["reason"])
        # excluding a lockfile adds no blocking: nothing here is an author gate
        self.assertEqual([g["kind"] for g in self.sz_run(self.SZ_LOCK)[0]["gates"] if g["kind"] == "author"], [])

    def test_global_path_exclusion_and_pr_text_are_not_evidence(self):
        files = self.SZ_VEND + self.SZ_GEN + self.SZ_HAND
        # a size_exclude glob list (the old design) has no effect at all
        result, gates = self.sz_run(files, policy={"size_exclude": ["agent-ui/cindy-43001ea/*", "cindy-parts.css"]})
        self.assertIn("size", gates)
        self.assertEqual(result["excluded_lines"], 0)
        # the description claiming upstream commit, sha256 identity, licence and reproducibility proves nothing
        body = "Vendored from cindy@43001eaa00000000000000000000000000000000, sha256 identical to upstream, MIT, output reproduced by node scripts/build-parts.js\n" + "pytest test/x.py passed"
        result, gates = self.sz_run(files, body=body)
        self.assertIn("size", gates)
        self.assertEqual(result["excluded_lines"], 0)
        # a record kept for another PR does not apply here
        result, gates = self.sz_run(files, exceptions=self.sz_exception(), number=110)
        self.assertIn("size", gates)

    def test_accepted_head_bound_qq_review_exempts_exactly_the_listed_files(self):
        files = self.SZ_VEND + self.SZ_GEN + self.SZ_LOCK + self.SZ_HAND
        result, gates = self.sz_run(files, exceptions=self.sz_exception())
        self.assertNotIn("size", gates)
        self.assertEqual(result["lines"], self.sz_total(files))  # the total stays visible
        self.assertEqual(result["counted_lines"], self.sz_total(self.SZ_HAND))
        self.assertEqual(result["excluded_lines"], self.sz_total(self.SZ_VEND + self.SZ_GEN + self.SZ_LOCK))
        self.assertTrue(result["size_exception"].startswith("applied"))
        self.assertEqual(sorted(f["path"] for f in result["excluded_files"]),
                         sorted(f["path"] for f in self.SZ_VEND + self.SZ_GEN + self.SZ_LOCK))
        # still a human check, and it says what the exception is not and whose judgments these are
        reason = gates["provenance"]["reason"]
        self.assertEqual(gates["provenance"]["kind"], "human")
        self.assertIn("agent-ui/cindy-43001ea/a.js (3000 lines, QQ-recorded exception, vendored)", reason)
        self.assertIn("cindy-parts.css (2991 lines, QQ-recorded exception, generated)", reason)
        self.assertIn("not a code approval or a merge permission", reason)
        self.assertIn("QQ's recorded judgments, not verified by this tool", reason)

    def test_generated_output_of_this_repo_needs_no_upstream_but_its_own_evidence(self):
        # only the generated group is recorded: no upstream repo / commit anywhere, and it is applied
        files = self.SZ_GEN + self.SZ_HAND
        exc = self.sz_exception(review={"files": ["cindy-parts.css"]})
        exc[self.SZ_KEY]["groups"] = [exc[self.SZ_KEY]["groups"][1]]
        self.assertNotIn("upstream", str(exc[self.SZ_KEY]["groups"][0]))
        result, gates = self.sz_run(files + [{"path": "scripts/clip.py", "additions": 2600, "deletions": 0}], exceptions=exc)
        self.assertTrue(result["size_exception"].startswith("applied"))
        self.assertEqual(result["excluded_lines"], 2991)
        # while a third-party copy without its upstream is refused
        vend_only = self.sz_exception(vendored={"upstream": {}})
        self.assertIn("vendored group not applied (no upstream repo)", self.sz_run(self.SZ_VEND + self.SZ_HAND, exceptions=vend_only)[0]["size_exception"])

    def test_size_exception_falls_back_to_counting_everything(self):
        files = self.SZ_VEND + self.SZ_GEN + self.SZ_LOCK + self.SZ_HAND
        lock_only = self.sz_total(files) - self.sz_total(self.SZ_LOCK)
        # evidence missing or malformed in one group: that group is counted in full, the other still applies
        broken = {
            "vendored: no licence": dict(vendored={"licence": ""}),
            "vendored: no upstream commit": dict(vendored={"upstream": {"repo": "good-night-oppie/cindy"}}),
            "vendored: mutable ref instead of a commit": dict(vendored={"upstream": {"repo": "good-night-oppie/cindy", "commit": "main"}}),
            "vendored: short commit": dict(vendored={"upstream": {"repo": "good-night-oppie/cindy", "commit": "43001eaa"}}),
            "vendored: no upstream repo": dict(vendored={"upstream": {"commit": "43001eaa" + "0" * 32}}),
            "vendored: byte identity not recorded": dict(vendored={"verified": {"method": "sha"}}),
            "vendored: byte identity failed": dict(vendored={"verified": {"byte_identical": False, "method": "sha"}}),
            "vendored: no exact file list": dict(vendored={"files": []}),
            "vendored: a glob instead of exact files": dict(vendored={"files": ["agent-ui/cindy-43001ea/*"]}),
            "vendored: a directory instead of exact files": dict(vendored={"files": ["agent-ui/cindy-43001ea/"]}),
            "vendored: unreadable upstream": dict(vendored={"upstream": "good-night-oppie/cindy@main"}),
            "generated: no input files": dict(generated={"generated": {"command": "node b.js", "result_sha256": {"cindy-parts.css": "a" * 64}, "reproduced": True, "record": "r"}}),
            "generated: no command": dict(generated={"generated": {"inputs": ["x.css"], "result_sha256": {"cindy-parts.css": "a" * 64}, "reproduced": True, "record": "r"}}),
            "generated: no result hash": dict(generated={"generated": {"inputs": ["x.css"], "command": "node b.js", "reproduced": True, "record": "r"}}),
            "generated: malformed result hash": dict(generated={"generated": {"inputs": ["x.css"], "command": "node b.js", "result_sha256": {"cindy-parts.css": "abc"}, "reproduced": True, "record": "r"}}),
            "generated: not reproducible": dict(generated={"generated": {"inputs": ["x.css"], "command": "node b.js", "result_sha256": {"cindy-parts.css": "a" * 64}, "reproduced": False, "record": "r"}}),
            "generated: reproduction record missing": dict(generated={"generated": {"inputs": ["x.css"], "command": "node b.js", "result_sha256": {"cindy-parts.css": "a" * 64}, "reproduced": True}}),
        }
        for name, over in broken.items():
            with self.subTest(name):
                result, gates = self.sz_run(files, exceptions=self.sz_exception(**over))
                bad_group = "vendored" if "vendored" in over else "generated"
                kept = {f["path"] for f in result["excluded_files"]}
                for f in (self.SZ_VEND if bad_group == "vendored" else self.SZ_GEN):
                    self.assertNotIn(f["path"], kept)  # the broken group is counted in full
                self.assertIn("agent-ui/package-lock.json", kept)  # the lockfile is still excluded by name
                self.assertIn(f"{bad_group} group not applied", result["size_exception"])
                self.assertIn("size", gates)  # still over the limit once those lines count
                reason = gates["provenance"]["reason"]
                # partially applied: the other group still is, and QQ is told WHICH group failed and WHY
                self.assertTrue(result["size_exception"].startswith("applied"), result["size_exception"])
                self.assertRegex(reason, rf"{bad_group} group not applied \(.+?\)")
                self.assertIn("the files of the group that is not applied are counted in full; the groups recorded completely stay applied", reason)
                self.assertRegex(reason, r"applied to \d+ file\(s\); " + bad_group + r" group not applied \(.+?\); the files of the group")
                self.assertNotIn("every line is counted", reason)  # the other group's files ARE excluded
                self.assertNotIn("normal counting is restored", reason)  # that wording is only for "nothing applied"

        with self.subTest("nothing applied: both groups broken"):
            exc = self.sz_exception(vendored={"licence": ""}, generated={"generated": {"inputs": ["x.css"], "command": "node b.js", "reproduced": False}})
            result, gates = self.sz_run(files, exceptions=exc)
            reason = gates["provenance"]["reason"]
            self.assertTrue(result["size_exception"].startswith("not applied"), result["size_exception"])
            self.assertEqual(result["excluded_lines"], self.sz_total(self.SZ_LOCK))
            self.assertIn("vendored group not applied (no licence recorded)", reason)
            self.assertIn("generated group not applied", reason)
            self.assertIn("normal counting is restored, only the defined lockfile exclusion stays", reason)
            self.assertNotIn("stay applied", reason)
            self.assertNotIn("recorded completely", reason)

        # the review itself is not trusted: everything is counted, only the lockfile stays out
        reviews = {
            "review verdict is rejected": dict(review={"verdict": "rejected"}),
            "review verdict is pending": dict(review={"verdict": "pending"}),
            "review verdict missing": dict(review={"verdict": ""}),
            "review not by QQ": dict(review={"by": "BB"}),
            "review source missing": dict(review={"source": ""}),
            "review head abbreviated": dict(review={"head": self.SZ_HEAD[:8]}),
            "review head changed after QQ looked": dict(review={"head": "deadbeef" + "0" * 32}),
            "review head missing": dict(review={"head": ""}),
            "review lists no files": dict(review={"files": []}),
            "review lists a glob": dict(review={"files": ["agent-ui/cindy-43001ea/*"]}),
        }
        for name, over in reviews.items():
            with self.subTest(name):
                result, gates = self.sz_run(files, exceptions=self.sz_exception(**over))
                self.assertEqual(result["excluded_lines"], self.sz_total(self.SZ_LOCK))
                self.assertEqual(result["counted_lines"], lock_only)
                self.assertTrue(result["size_exception"].startswith("not applied"), result["size_exception"])
                self.assertIn("size", gates)
                self.assertIn("QQ size exception not applied", gates["provenance"]["reason"])

        with self.subTest("abbreviated head is refused because it is abbreviated"):
            result, _ = self.sz_run(files, exceptions=self.sz_exception(review={"head": self.SZ_HEAD[:8]}))
            self.assertIn("exact 40-hex", result["size_exception"])
        with self.subTest("report wording: normal counting is restored, only the lockfile exclusion stays"):
            result, gates = self.sz_run(files, exceptions=self.sz_exception(review={"head": "deadbeef" + "0" * 32}))
            reason = gates["provenance"]["reason"]
            self.assertIn("normal counting is restored, only the defined lockfile exclusion stays", reason)
            self.assertNotIn("every line is counted", reason)
            self.assertEqual(result["counted_lines"], self.sz_total(files) - self.sz_total(self.SZ_LOCK))  # 10276, not 11316

        # a record that cannot be read trustworthily never raises and never exempts anything
        unreadable = {
            "deviating is not a list (passes every explicit check, then fails)": self.sz_exception(
                vendored={"verified": {"byte_identical": True, "method": "sha", "deviating": 5}})[self.SZ_KEY],
            "record is a string": "QQ said yes",
            "review is a list": {"review": ["QQ"], "groups": []},
            "groups is a dict": {"review": self.sz_exception()[self.SZ_KEY]["review"], "groups": {"kind": "vendored"}},
            "groups is empty": {"review": self.sz_exception()[self.SZ_KEY]["review"], "groups": []},
            "group is a string": {"review": self.sz_exception()[self.SZ_KEY]["review"], "groups": ["vendored"]},
        }
        for name, record in unreadable.items():
            with self.subTest(name):
                result, gates = self.sz_run(files, exceptions={self.SZ_KEY: record})
                self.assertEqual(result["excluded_lines"], self.sz_total(self.SZ_LOCK))
                self.assertIn("size", gates)
                self.assertTrue(result["size_exception"].startswith("not applied"), result["size_exception"])
                if name.startswith("deviating"):
                    self.assertEqual(result["size_exception"], "not applied: record unreadable")
        with self.subTest("policy size_exceptions itself is not a mapping"):
            self.assertIn("size", self.sz_run(files, exceptions=["good-night-oppie/MontClip#109"])[1])

    def test_exception_binds_to_the_full_40_hex_head_only(self):
        files = self.SZ_VEND + self.SZ_GEN + self.SZ_LOCK + self.SZ_HAND
        lock = self.sz_total(self.SZ_LOCK)
        new_head = "aaaaaaaa" + "0" * 32
        # a short prefix (even one that is a prefix of the real head) is never an authorization match
        prefixes = {"8-char prefix": self.SZ_HEAD[:8], "12-char prefix": self.SZ_HEAD[:12], "39 of 40 chars": self.SZ_HEAD[:39]}
        for name, head in prefixes.items():
            with self.subTest(name):
                result, gates = self.sz_run(files, exceptions=self.sz_exception(review={"head": head}))
                self.assertIn("exact 40-hex", result["size_exception"])
                self.assertEqual(result["excluded_lines"], lock)
                self.assertIn("size", gates)
        # a different commit that shares the first 8 chars is a different head; the shown prefixes tell them apart
        look_alike = self.SZ_HEAD[:8] + "1" * 32
        result, gates = self.sz_run(files, exceptions=self.sz_exception(review={"head": look_alike}))
        self.assertEqual(result["excluded_lines"], lock)
        reviewed, current = re.search(r"reviewed (\w+) but the PR head is (\w+)", result["size_exception"]).groups()
        self.assertNotEqual(reviewed, current)
        self.assertGreater(len(reviewed), 8)  # 8 chars would have shown two identical strings
        # the full head is accepted
        result, _ = self.sz_run(files, exceptions=self.sz_exception())
        self.assertTrue(result["size_exception"].startswith("applied"))
        # a new head invalidates the same record, and the report says what happens instead
        result, gates = self.sz_run(files, exceptions=self.sz_exception(), head=new_head)
        self.assertIn("stale: reviewed 1fcbed24 but the PR head is aaaaaaaa", result["size_exception"])
        self.assertEqual(result["excluded_lines"], lock)
        self.assertIn("normal counting is restored, only the defined lockfile exclusion stays", gates["provenance"]["reason"])
        # a review of the new full head applies again
        result, _ = self.sz_run(files, exceptions=self.sz_exception(review={"head": new_head}), head=new_head)
        self.assertTrue(result["size_exception"].startswith("applied"))
        # an abbreviated head on the PR side is not a head either
        result, _ = self.sz_run(files, exceptions=self.sz_exception(), head=self.SZ_HEAD[:8])
        self.assertEqual(result["size_exception"], "not applied: PR head unknown")

    def test_size_exception_does_not_cover_deviating_or_unreviewed_or_new_files(self):
        files = self.SZ_VEND + self.SZ_GEN + self.SZ_LOCK + self.SZ_HAND
        # a third-party file that deviates from its upstream is counted in full, the identical one is still exempt
        exc = self.sz_exception(vendored={"verified": {"byte_identical": True, "method": "sha", "deviating": ["agent-ui/cindy-43001ea/a.js"]}})
        result, gates = self.sz_run(files, exceptions=exc)
        kept = {f["path"] for f in result["excluded_files"]}
        self.assertNotIn("agent-ui/cindy-43001ea/a.js", kept)
        self.assertIn("agent-ui/cindy-43001ea/b.js", kept)
        self.assertEqual(result["counted_lines"], self.sz_total(self.SZ_HAND) + 3000)
        self.assertIn("size", gates)
        # a path added to the vendored directory but not on the list is counted in full
        added = [{"path": "agent-ui/cindy-43001ea/new.js", "additions": 2600, "deletions": 0}]
        result, gates = self.sz_run(files + added, exceptions=self.sz_exception())
        self.assertNotIn("agent-ui/cindy-43001ea/new.js", {f["path"] for f in result["excluded_files"]})
        self.assertEqual(result["counted_lines"], self.sz_total(self.SZ_HAND) + 2600)
        self.assertIn("size", gates)
        # per-file scope: a file in the group's list that QQ's review does not list is counted in full
        exc = self.sz_exception(review={"files": ["agent-ui/cindy-43001ea/b.js", "cindy-parts.css"]})
        result, gates = self.sz_run(files, exceptions=exc)
        kept = {f["path"] for f in result["excluded_files"]}
        self.assertNotIn("agent-ui/cindy-43001ea/a.js", kept)
        self.assertIn("agent-ui/cindy-43001ea/b.js", kept)
        self.assertIn("size", gates)
        # a reviewed file that is not in this PR any more exempts nothing
        gone = self.sz_exception(vendored={"files": ["agent-ui/cindy-43001ea/gone.js"]}, review={"files": ["agent-ui/cindy-43001ea/gone.js", "cindy-parts.css"]})
        result, gates = self.sz_run(files, exceptions=gone)
        self.assertNotIn("agent-ui/cindy-43001ea/a.js", {f["path"] for f in result["excluded_files"]})

    def test_exceptions_apply_only_from_a_confirmed_controlled_entry(self):
        files = self.SZ_VEND + self.SZ_GEN + self.SZ_LOCK + self.SZ_HAND
        lock = self.sz_total(self.SZ_LOCK)
        record = self.sz_exception()
        # a perfect record is still ignored while its origin is not attested (the flag is absent in config defaults)
        self.assertIs(POLICY.get("size_exceptions_trusted_source"), False)
        result = triage(self.sz_pr(files), dict(POLICY, size_exceptions=record))
        self.assertIn("origin not confirmed", result["size_exception"])
        self.assertEqual(result["excluded_lines"], lock)
        gates = {g["id"]: g for g in result["gates"]}
        self.assertIn("size", gates)
        self.assertIn("origin not confirmed", gates["provenance"]["reason"])  # QQ is told why nothing was applied
        # only the boolean True counts: other truthy values do not attest anything
        for name, value in {"False": False, "None": None, "string": "yes", "one": 1, "dict": {"confirmed": True}}.items():
            with self.subTest(name):
                result = triage(self.sz_pr(files), dict(POLICY, size_exceptions=record, size_exceptions_trusted_source=value))
                self.assertIn("origin not confirmed", result["size_exception"])
                self.assertEqual(result["excluded_lines"], lock)
        # attested by the controlled entry: applied
        self.assertTrue(self.sz_run(files, exceptions=record)[0]["size_exception"].startswith("applied"))
        # nothing the PR carries can supply a record: description, files in the PR, extra fields on the PR object
        injected = json.dumps({self.SZ_KEY: record[self.SZ_KEY]})
        pr = self.sz_pr(files + [{"path": "size_exceptions.json", "additions": 1, "deletions": 0},
                                 {"path": ".gitattributes", "additions": 1, "deletions": 0}], body=injected + "\npytest test/x.py passed")
        pr["changedFiles"] = len(pr["files"])
        pr.update(size_exceptions=record, review=record[self.SZ_KEY]["review"], groups=record[self.SZ_KEY]["groups"])
        result = triage(pr, dict(POLICY, size_exceptions_trusted_source=True))  # attested, but no record in policy
        self.assertIsNone(result["size_exception"])
        self.assertEqual(result["excluded_lines"], lock)
        self.assertIn("size", {g["id"]: g for g in result["gates"]})

    def test_incomplete_file_list_counts_everything(self):
        # gh caps the file list at 100: more files than it returned => nothing is excluded, not even a lockfile
        files = self.SZ_VEND[:1] + self.SZ_LOCK + self.SZ_HAND
        for name, extra in {"paginated: 250 files, 4 returned": {"changedFiles": 250}, "query failed: no files returned": {"changedFiles": 5, "drop_files": True}}.items():
            with self.subTest(name):
                result, gates = self.sz_run(files, exceptions=self.sz_exception(), **extra)
                self.assertEqual(result["excluded_lines"], 0)
                self.assertIn("file list is incomplete", gates["provenance"]["reason"])
                self.assertIn("file list incomplete, all lines counted", gates["provenance"]["reason"])
                self.assertIn("no file is excluded", gates["provenance"]["reason"])  # a lockfile cannot be told apart either
                self.assertNotIn("only the defined lockfile exclusion stays", gates["provenance"]["reason"])
        # a PR that genuinely changes no files has nothing to check
        none = self.sz_pr([])
        none["changedFiles"] = 0
        self.assertNotIn("provenance", {g["id"]: g for g in triage(none, POLICY)["gates"]})

    def test_provenance_gate_is_for_a_maintainer_not_a_bounce_to_the_author(self):
        gates = triage(self.sz_pr(self.SZ_LOCK), POLICY)["gates"]
        self.assertEqual([g["kind"] for g in gates if g["id"] == "provenance"], ["human"])
        self.assertFalse([g for g in gates if g["kind"] == "author"])  # nothing here bounces the PR to needs_author
        plain = triage(self.sz_pr([{"path": "scripts/clip.py", "additions": 10, "deletions": 1}]), POLICY)["gates"]
        self.assertNotIn("provenance", [g["id"] for g in plain])

    def test_ai_signal(self):
        pr = snapshot(body="pytest ok\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")
        self.assertIn("Claude Code", triage(pr, POLICY)["ai_signals"])


class ParsingTest(unittest.TestCase):
    def test_slack_link_formats(self):
        text = ("<@U1> pls review <https://github.com/sgl-project/sglang/pull/123|#123> and "
                "https://github.com/other/repo/pull/9 and github.com/sgl-project/sglang/pull/123")
        self.assertEqual(github.find_pr_refs(text, {"sgl-project/sglang"}), [("sgl-project/sglang", 123)])

    def test_summarize_checks(self):
        summary = github.summarize_checks([
            check("a"), check("b", conclusion="FAILURE"), check("c", status="IN_PROGRESS", conclusion=None),
            check("d", conclusion="SKIPPED"),
            {"__typename": "StatusContext", "context": "ext", "state": "ERROR", "targetUrl": ""}])
        self.assertEqual(summary["counts"], {"success": 1, "failure": 2, "pending": 1, "skipped": 1,
                                             "cancelled": 0, "gated": 0, "other": 0})

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_superseded_cancelled_and_gate_are_not_failures(self):
        old = dict(check("unit", conclusion="CANCELLED"), completedAt="2026-10-30T01:00:00Z")
        new = dict(check("unit"), completedAt="2026-10-30T02:00:00Z")
        gate = dict(check("call-gate / pr-gate", conclusion="FAILURE"), completedAt="2026-10-30T01:00:00Z")
        lint = dict(check("lint", conclusion="CANCELLED"), completedAt="2026-10-30T01:00:00Z")
        summary = github.summarize_checks([old, new, gate, lint])
        self.assertEqual((summary["counts"]["success"], summary["counts"]["failure"],
                          summary["counts"]["gated"], summary["counts"]["cancelled"]), (1, 0, 1, 1))
        result = ci.assess({"review": {}}, snapshot(labels=["run-ci"], rollup=[old, new, lint]))
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["passed"])
        self.assertEqual([s["command"] for s in result["suggestions"]], ["/rerun-failed-ci"])
        self.assertEqual(ci.assess({"review": {}}, snapshot(rollup=[new, gate]))["status"], "gated")
        finish = dict(check("pr-test-finish", conclusion="FAILURE"), completedAt="2026-10-30T03:00:00Z")
        self.assertEqual(github.summarize_checks([new, lint, finish])["counts"]["failure"], 0)  # cascade
        self.assertEqual(github.summarize_checks([new, finish])["counts"]["failure"], 1)  # only signal
        real = dict(check("stage-b-test (rocm10, mi300, 2)", conclusion="FAILURE"), completedAt="2026-10-30T03:00:00Z")
        waiter = dict(check("wait-for-stage-b-amd", conclusion="FAILURE"), completedAt="2026-10-30T03:00:00Z")
        self.assertEqual([f["name"] for f in github.summarize_checks([real, waiter])["failures"]],
                         ["PR Test / stage-b-test (rocm10, mi300, 2)"])

    def test_hunk_ranges(self):
        diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -10,3 +10,5 @@ def f():\n"
                "+a\n+b\n context\n--- a/y.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n")
        ranges = new_side_ranges(diff)
        self.assertEqual(ranges, {"x.py": [[10, 14]]})
        self.assertTrue(anchored({"path": "x.py", "line": 12}, ranges))
        self.assertFalse(anchored({"path": "x.py", "line": 30}, ranges))


class CITest(unittest.TestCase):
    card = {"review": {"targeted_tests": ["test_y.py"]}}

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_missing_label_and_targeted(self):
        result = ci.assess(self.card, snapshot())
        commands = [s["command"] for s in result["suggestions"]]
        self.assertEqual(commands, ["/tag-and-rerun-ci", "/rerun-test test_y.py"])
        self.assertEqual(result["status"], "not_started")
        self.assertFalse(result["passed"])

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_failures_need_triage_then_classify(self):
        rollup = [check("ok"), check("unit", conclusion="FAILURE"), check("gpu", conclusion="FAILURE")]
        snap = snapshot(labels=["run-ci"], rollup=rollup)
        first = ci.assess(self.card, snap)
        self.assertTrue(first["needs_triage"])
        self.assertFalse(first["passed"])
        sig = ci.signature(first["checks"]["failures"])
        names = [f["name"] for f in first["checks"]["failures"]]
        triage_record = {"signature": sig, "failures": [
            {"name": names[0], "classification": "unrelated", "evidence": "x"},
            {"name": names[1], "classification": "infra", "evidence": "runner lost"}]}
        second = ci.assess(self.card, snap, triage_record)
        self.assertFalse(second["needs_triage"])
        self.assertFalse(second["passed"])  # infra failure still needs its one retry
        self.assertIn("rerun-failed", [s["family"] for s in second["suggestions"]])
        triage_record["failures"][1]["classification"] = "unrelated"
        self.assertTrue(ci.assess(self.card, snap, triage_record)["passed"])

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_optional_lane_gates_do_not_hold_ci(self):
        def gate(workflow):
            return {"__typename": "CheckRun", "workflowName": workflow, "name": "call-gate / pr-gate",
                    "status": "COMPLETED", "conclusion": "FAILURE", "detailsUrl": ""}

        rollup = [check("unit"), gate("PR Test Extra"), gate("PR Test Extra (AMD)"), gate("PR Test (MLU)")]
        result = ci.assess({"review": {}}, snapshot(labels=["run-ci"], rollup=rollup))
        self.assertEqual((result["status"], result["passed"]), ("green", True))
        self.assertEqual(result["checks"]["counts"]["not_requested"], 3)
        held = ci.assess({"review": {}}, snapshot(labels=["run-ci"], rollup=rollup + [gate("PR Test Base")]))
        self.assertEqual((held["status"], held["passed"]), ("gated", False))  # the base lane never ran
        unlabeled = ci.assess({"review": {}}, snapshot(rollup=rollup))
        self.assertEqual(unlabeled["status"], "gated")  # no run-ci: nothing was allowed to run

    def test_related_failure_blocks(self):
        snap = snapshot(labels=["run-ci"], rollup=[check("unit", conclusion="FAILURE")])
        sig = ci.signature(github.summarize_checks(snap["statusCheckRollup"])["failures"])
        record = {"signature": sig, "failures": [{"name": "PR Test / unit", "classification": "related", "evidence": "e"}]}
        result = ci.assess(self.card, snap, record)
        self.assertEqual([f["classification"] for f in result["blocking"]], ["related"])


class VerificationTest(unittest.TestCase):
    def test_verdict_recomputed(self):
        findings = [{"severity": "blocker"}, {"severity": "minor"}]
        kept, dropped = agent.apply_verification(findings, {"verdicts": [
            {"index": 0, "verdict": "rejected", "reason": "guarded"},
            {"index": 1, "verdict": "confirmed", "reason": "yes"}]})
        self.assertEqual(len(dropped), 1)
        self.assertEqual(agent.effective_verdict({"verdict": "changes_needed"}, kept), "minor")
        kept2, _ = agent.apply_verification([{"severity": "major"}], {"verdicts": [
            {"index": 0, "verdict": "plausible", "reason": "?"}]})
        self.assertEqual(agent.effective_verdict({"verdict": "changes_needed"}, kept2), "needs_human")


class SkillTest(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, repo, text):
        (repo / "skills/sglang-humanize-review/SKILL.md").write_text(text)
        self.git(repo, "add", ".")
        self.git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", text)
        return self.git(repo, "rev-parse", "HEAD")

    def test_sync_tracks_branch_and_tolerates_a_brief_outage(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin"
            (origin / "skills/sglang-humanize-review").mkdir(parents=True)
            self.git(origin, "init", "-q", "-b", "main")
            first = self.commit(origin, "v1")
            config = {"data_dir": tmp, "agent": {"review_skill": {
                "repo": f"file://{origin}", "branch": "main", "subdir": "skills/sglang-humanize-review",
                "sync_seconds": 0, "max_stale_seconds": 3600}}}
            sync = SkillSync(config)
            skill_dir, commit = sync.ensure()
            self.assertEqual(commit, first)
            second = self.commit(origin, "v2")
            self.assertEqual(sync.ensure()[1], second)  # always the latest branch tip
            self.assertEqual((skill_dir / "SKILL.md").read_text(), "v2")
            sync.url = f"file://{tmp}/gone"
            subprocess.run(["git", "-C", str(sync.clone), "remote", "set-url", "origin", sync.url], check=True)
            self.assertEqual(sync.ensure()[1], second)  # brief outage: keep the fresh checkout
            sync.synced_at -= 7200
            with self.assertRaises(SkillSyncError):
                sync.ensure()  # stale beyond max_stale: refuse to review with an old skill

    def test_missing_steps_come_from_executed_commands_only(self):
        with tempfile.TemporaryDirectory() as skill_dir:
            (Path(skill_dir) / "references").mkdir()
            (Path(skill_dir) / "references/maintainer-standards.md").write_text("x")
            prompt_echo = f"run python3 {skill_dir}/scripts/summarize_sglang_review_corpus.py and read {skill_dir}/SKILL.md"
            # jj-agent overlay: the sweep is required only when the skill ships the script.
            self.assertEqual(missing_skill_steps(prompt_echo, skill_dir), [
                "read SKILL.md", "read references/maintainer-standards.md"])
            log = (f"/bin/zsh -lc 'cat {skill_dir}/SKILL.md'\n"
                   f"/bin/zsh -lc \"sed -n 1,200p {skill_dir}/references/maintainer-standards.md\"\n"
                   f"/bin/zsh -lc 'python3 {skill_dir}/scripts/summarize_sglang_review_corpus.py --path x'\n")
            self.assertEqual(missing_skill_steps(log, skill_dir), [])
            other_copy = log.replace(skill_dir, "/Users/x/.claude/skills/sglang-humanize-review")
            self.assertIn("read SKILL.md", missing_skill_steps(other_copy, skill_dir))  # must be the synced copy


class AgentProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_module.load(None)
        self.config["data_dir"] = self.tmp.name
        self.config["agent"].update(service_tier="priority", profiles={
            "T2": {"model": "gpt-6.1-sol", "effort": "high"}, "T3": {"model": "gpt-6-astra", "effort": "xhigh"},
            "verify": {"model": "gpt-6.1-sol", "effort": "high"}})
        self.ws = FakeWorkspace(self.config, "o/r", 1, HEAD, "main")
        self.ws.repo, self.ws.number, self.ws.worktree = "o/r", 1, Path(self.tmp.name)

    def codex_cmd(self, job):
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            Path(cmd[cmd.index("-o") + 1]).write_text('{"ok": true}')
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(agent, "_run_tree", side_effect=fake_run):
            _, info = agent.run_agent(self.config, "p", {}, self.ws, job, agent.profile_for(self.config, job))
        return seen["cmd"], info

    def test_tier_profiles_and_fast_tier_reach_codex(self):
        cmd, info = self.codex_cmd("T3")
        self.assertEqual(cmd[cmd.index("-m") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="xhigh"', cmd)
        self.assertIn('service_tier="priority"', cmd)
        self.assertEqual((info["model"], info["effort"]), ("gpt-6-astra", "xhigh"))
        cmd, _ = self.codex_cmd("T1")  # no profile: falls back to the runner's own config
        self.assertNotIn("-m", cmd)
        self.assertFalse(any("model_reasoning_effort" in part for part in cmd))

    def test_only_blocking_findings_are_verified(self):
        findings = [{"severity": "nit"}, {"severity": "major"}, {"severity": "minor"}, {"severity": "blocker"}]
        sent = {}

        def fake_run_agent(config, prompt, schema, workspace, label, profile=None):
            sent["prompt"] = prompt
            return {"verdicts": [{"index": 0, "verdict": "rejected", "reason": "x"},
                                 {"index": 1, "verdict": "confirmed", "reason": "y"}]}, {"label": label}

        with mock.patch.object(agent, "run_agent", side_effect=fake_run_agent):
            verification, _ = agent.verify(self.config, self.ws, findings)
        by_index = {v["index"]: v["verdict"] for v in verification["verdicts"]}
        self.assertEqual(by_index, {1: "rejected", 3: "confirmed", 0: "unverified", 2: "unverified"})
        self.assertNotIn('"nit"', sent["prompt"])
        kept, dropped = agent.apply_verification(findings, verification)
        self.assertEqual([f["severity"] for f in dropped], ["major"])
        self.assertEqual(agent.effective_verdict({"verdict": "changes_needed"}, kept), "changes_needed")
        with mock.patch.object(agent, "run_agent") as never:
            verification, info = agent.verify(self.config, self.ws, [{"severity": "nit"}])
        never.assert_not_called()
        self.assertTrue(info["skipped"])


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_module.load(None)
        self.config.update(data_dir=self.tmp.name, notify_macos=False)
        self.config["agent"]["review_skill"]["repo"] = ""  # no network in tests
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        self.addCleanup(self.store.conn.close)
        usage = mock.patch.object(agent, "usage_hook", None)
        usage.start()
        self.addCleanup(usage.stop)
        self.pipe = Pipeline(self.config, self.store)
        self.pipe.submit = lambda key, fn, *args: fn(*args)  # run jobs inline
        self.snap = snapshot()
        self.approved = set()
        self.writes = []
        patches = [
            mock.patch.object(github, "fetch_pr", side_effect=lambda repo, n: self.snap),
            mock.patch.object(github, "approved_heads", side_effect=lambda *a: self.approved),
            mock.patch.object(github, "comment", side_effect=self.fake_comment),
            mock.patch.object(github, "submit_review", side_effect=self.fake_review),
            mock.patch.object(agent, "Workspace", FakeWorkspace),
            mock.patch.object(agent, "review", side_effect=self.fake_agent_review),
            mock.patch.object(agent, "verify", side_effect=lambda c, w, f: (
                {"verdicts": [{"index": i, "verdict": "rejected", "reason": "no"} for i in range(len(f))]},
                {"label": "verify"})),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.store.ensure_pr("sgl-project/sglang", 7, requester={
            "id": "slack:C1:1", "source": "slack", "author": "alice", "ts": 1})
        self.key = "sgl-project/sglang#7"

    def fake_comment(self, repo, number, body):
        self.writes.append(("comment", body))
        return "https://github.com/c/1"

    def fake_review(self, repo, number, commit_id, event, body, comments=()):
        self.writes.append((event, commit_id, body, list(comments)))
        if event == "APPROVE":  # Explicit human API approval is now visible on GitHub.
            self.approved.add(commit_id)
        return {"id": 1, "url": "https://github.com/r/1"}

    def fake_agent_review(self, config, workspace, tier, *args):
        return ({"summary": "s", "mermaid": "", "corpus_synthesis": "", "verdict": "lgtm",
                 "verdict_reason": "", "test_gaps": [], "related_tests": ["test_y.py"],
                 "targeted_tests": [], "approve_note": "LGTM.", "coverage_notes": "",
                 "findings": [{"severity": "nit", "category": "style", "path": "a.py", "line": 1,
                               "title": "t", "scenario": "s", "evidence": "", "fix": "", "comment": "Nit: x"}]},
                {"label": "review", "seconds": 1})

    def advance(self):
        self.pipe.advance(self.store.get_pr(self.key))
        return self.store.get_pr(self.key)

    def test_changes_requested_retracts_approval_on_same_head(self):
        self.addCleanup(self.store.conn.close)  # Windows: close SQLite before TemporaryDirectory cleanup
        self.assertIn("reviews", github.PR_FIELDS.split(","))  # latestReviews can omit commit.oid
        card = self.advance()
        self.assertEqual(card["review_head"], HEAD)
        self.store.update_pr(self.key, stage="approved",
                             decision=dict(card["decision"], approved_head=HEAD))
        self.approved = {HEAD}  # GitHub REST still contains the historical APPROVED review
        self.snap["reviewDecision"] = "CHANGES_REQUESTED"
        self.snap["latestReviews"] = [{"author": {"login": "BBuf"}, "state": "CHANGES_REQUESTED", "commit": {}}]
        self.snap["reviews"] = [
            {"author": {"login": "BBuf"}, "state": "APPROVED", "commit": {"oid": HEAD}, "submittedAt": "2026-11-05T17:16:20Z"},
            {"author": {"login": "BBuf"}, "state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}, "submittedAt": "2026-11-06T05:24:10Z"},
        ]
        card = self.advance()
        self.assertEqual(card["stage"], "needs_author")
        self.assertNotEqual(card["decision"].get("approved_head"), HEAD)
        self.assertFalse(card["decision"].get("merge_ready", False))
        self.assertNotIn(("APPROVE", HEAD), [w[:2] for w in self.writes])

    def test_changes_requested_on_old_head_waits_for_rereview(self):
        self.addCleanup(self.store.conn.close)
        card = self.advance()
        self.store.update_pr(
            self.key, stage="queued", review=dict(card["review"], head=NEW_HEAD), review_head=NEW_HEAD,
            decision=dict(card["decision"], approved_head=HEAD))
        self.approved = {HEAD}
        self.snap = snapshot(head=NEW_HEAD)
        self.snap["reviewDecision"] = "CHANGES_REQUESTED"  # GitHub keeps this after the author pushes
        self.snap["reviews"] = [
            {"author": {"login": "BBuf"}, "state": "CHANGES_REQUESTED", "commit": {"oid": HEAD}, "submittedAt": "2026-11-05T17:13:38Z"},
            {"author": {"login": "BBuf"}, "state": "COMMENTED", "commit": {"oid": NEW_HEAD}, "submittedAt": "2026-11-06T17:39:56Z"},
        ]
        card = self.advance()
        self.assertEqual(card["stage"], "ready")
        self.assertEqual(card["decision"].get("ready_kind"), "approve")
        self.assertFalse(card["decision"].get("merge_ready", False))

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_full_flow_shadow_then_approve(self):
        card = self.advance()  # triage → review job runs inline
        self.assertEqual(card["review_head"], HEAD)
        self.assertEqual(card["review"]["verdict"], "lgtm")  # nit rejected by verifier
        card = self.advance()  # shadow: the approval is yours, right after review, CI or not
        self.assertEqual((card["stage"], card["decision"]["ready_kind"]), ("ready", "approve"))
        self.assertEqual(self.writes, [])
        self.assertEqual(card["ci"]["suggestions"][0]["state"], "todo")  # CI shown, not sent

        self.pipe.approve(self.key)
        self.assertEqual(self.writes[0][:2], ("APPROVE", HEAD))
        self.assertEqual(self.writes[1], ("comment", "/tag-and-rerun-ci"))  # approve + run-ci
        self.assertEqual(self.store.get_pr(self.key)["stage"], "approved")
        with self.assertRaises(ValueError):
            self.pipe.approve(self.key)  # once per head
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit"), check("gpu")])
        card = self.advance()
        self.assertEqual((card["stage"], card["stage_reason"]),
                         ("approved", "CI passed, GitHub still needs a code-owner approval"))

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_live_mode_sends_ci_once_and_new_push_requeues(self):
        self.config["mode"] = "live"
        self.advance()
        self.advance()
        self.advance()
        self.assertEqual([w for w in self.writes if w[0] == "comment"], [("comment", "/tag-and-rerun-ci")])
        self.snap = snapshot(head=NEW_HEAD)
        card = self.advance()
        self.assertEqual(card["review_head"], NEW_HEAD)  # re-reviewed on the new head
        with self.assertRaises(ValueError):
            self.store.update_pr(self.key, review_head=HEAD)
            self.pipe.approve(self.key)  # stale review cannot be approved

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_green_ci_with_untriggered_targeted_tests_is_not_merge_ready(self):
        original = self.fake_agent_review

        def with_targeted(config, workspace, tier, *args):
            result, info = original(config, workspace, tier)
            return dict(result, targeted_tests=["test_y.py"]), info

        with mock.patch.object(agent, "review", side_effect=with_targeted):
            self.advance()
        self.pipe.approve(self.key)
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit")])
        card = self.advance()
        self.assertEqual((card["stage"], card["decision"]["ready_kind"]), ("ready", "ci"))
        self.assertIn("/rerun-test", card["stage_reason"])
        self.assertFalse(card["decision"]["merge_ready"])

    def test_agent_errors_retry_with_backoff_forever(self):
        self.pipe.submit = Pipeline.submit.__get__(self.pipe)  # real wrapper
        self.pipe.executor = SyncExecutor()
        with mock.patch.object(agent, "review", side_effect=agent.AgentError("boom")):
            for _ in range(5):
                card = self.advance()
        self.assertEqual(card["stage"], "error")
        self.assertEqual(card["decision"]["error_count"], 5)
        self.assertFalse(self.pipe.refresh_due(card))  # backing off
        self.store.update_pr(self.key, refreshed_at=time.time() - 3600)
        self.assertTrue(self.pipe.refresh_due(self.store.get_pr(self.key)))  # never parked on you

    def test_pr_merged_while_job_waits_is_not_reviewed_and_closes(self):
        self.addCleanup(self.store.conn.close)
        self.pipe.submit = Pipeline.submit.__get__(self.pipe)  # real wrapper
        waiting = []

        class QueueExecutor:
            def submit(self, fn, *args):
                waiting.append(fn)

        self.pipe.executor = QueueExecutor()
        card = self.advance()  # review job queued behind busy workers
        self.assertEqual(card["stage"], "queued")
        self.assertIn(self.key, self.pipe.inflight)
        self.snap = snapshot(state="MERGED")  # merged on GitHub while it waits
        with mock.patch.object(agent, "review", side_effect=AssertionError("must not review a merged PR")):
            waiting.pop()()
        self.assertNotIn(self.key, self.pipe.inflight)
        card = self.store.get_pr(self.key)
        self.assertTrue(card["decision"]["refresh"])
        self.assertTrue(self.pipe.refresh_due(card))
        self.assertEqual(self.advance()["stage"], "closed")

    def test_spend_cap_is_one_status_not_a_wall_of_errors(self):
        self.pipe.submit = Pipeline.submit.__get__(self.pipe)
        self.pipe.executor = SyncExecutor()
        capped = agent.AgentError("review: codex exit 1: ...\nERROR: You hit your spend cap set by the owner of "
                                  "your workspace. Ask an owner to increase your spend cap to continue.")
        with mock.patch.object(agent, "review", side_effect=capped):
            card = self.advance()
        self.assertEqual(card["stage_reason"], "failure #1: every codex account is out of quota, retrying automatically")
        self.assertEqual(self.pipe.status["agent_blocked"]["reason"], "every codex account is out of quota")
        self.store.update_pr(self.key, refreshed_at=0)
        self.advance()  # the cap was raised: the next job clears the banner
        self.assertNotIn("agent_blocked", self.pipe.status)

    def test_gate_bounces_to_author(self):
        self.snap = snapshot(body="")  # no test plan: still an author gate in this config
        self.assertEqual(self.advance()["stage"], "needs_author")
        self.store.update_pr(self.key, requesters=[{"id": "github-mention:x", "source": "github-mention"}])
        self.snap = snapshot(draft=True)  # a draft nobody asked about in Slack waits
        self.assertEqual(self.advance()["stage"], "needs_author")


class AutonomousTest(unittest.TestCase):
    """Live mode with every autonomous write you authorized; merge stays manual."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_module.load(None)
        self.config.update(data_dir=self.tmp.name, notify_macos=False, mode="live")
        self.config["agent"]["review_skill"]["repo"] = ""  # no network in tests
        self.config["policy"].update(auto_approve=True, auto_publish_findings=True, auto_author_notes=True,
                                     slack_ack=True, slack_report=True)
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        self.addCleanup(self.store.conn.close)
        self.store.set_kv("mode", "live")
        usage = mock.patch.object(agent, "usage_hook", None)
        usage.start()
        self.addCleanup(usage.stop)
        self.pipe = Pipeline(self.config, self.store)
        self.pipe.submit = lambda key, fn, *args: fn(*args)
        self.slack = FakeSlack()
        self.pipe.slack_for = lambda team_id: self.slack
        self.pipe.slack_teams = lambda: {"T1"}
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit")])
        self.verdict, self.findings, self.targeted = "lgtm", [], []
        self.writes = []
        self.approved = set()
        self.rerun = {"state": "missing", "url": "", "runs": []}
        self.held, self.targeted_jobs, self.discussion = [], [], []
        self.push_target = {"url": "https://github.com/someone/sglang.git", "branch": "b", "fork": "someone/sglang"}
        self.failed_step = "Run test"
        patches = [
            mock.patch.object(github, "push_target", side_effect=lambda *a: self.push_target),
            mock.patch.object(github, "failed_step", side_effect=lambda url: self.failed_step),
            mock.patch.object(github, "discussion", side_effect=lambda *a: list(self.discussion)),
            mock.patch.object(github, "reply_in_thread",
                              side_effect=lambda r, n, cid, body: self.writes.append(("reply", cid, body)) or "u"),
            mock.patch.object(github, "runs_awaiting_approval", side_effect=lambda *a: self.held),
            mock.patch.object(github, "approve_run", side_effect=lambda r, run_id: self.writes.append(("approve-run", run_id))),
            mock.patch.object(github, "run_failed_jobs", side_effect=lambda url: self.targeted_jobs),
            mock.patch.object(github, "fetch_pr", side_effect=lambda repo, n: self.snap),
            mock.patch.object(github, "approved_heads", side_effect=lambda *a: self.approved),
            mock.patch.object(github, "comment", side_effect=lambda r, n, body: self.writes.append(("comment", body)) or "u"),
            mock.patch.object(github, "submit_review", side_effect=self.fake_review),
            mock.patch.object(github, "merge", side_effect=lambda *a: self.writes.append(("merge",))),
            mock.patch.object(github, "rerun_test_status", side_effect=lambda *a: self.rerun),
            mock.patch.object(agent, "Workspace", FakeWorkspace),
            mock.patch.object(agent, "review", side_effect=self.fake_agent_review),
            mock.patch.object(agent, "verify", side_effect=lambda c, w, f: (
                {"verdicts": [{"index": i, "verdict": "confirmed", "reason": "ok"} for i in range(len(f))]}, {})),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.store.ensure_pr("sgl-project/sglang", 7, requester={
            "id": "slack:T1:C1:1.0", "source": "slack", "author": "alice", "team_id": "T1", "user_id": "U9",
            "channel_id": "C1", "thread_ts": "1.0", "lang": "en", "ts": 1})
        self.key = "sgl-project/sglang#7"



    def fake_review(self, repo, number, commit_id, event, body, comments=()):
        self.writes.append((event, commit_id, body, list(comments)))
        if event == "APPROVE":  # Explicit human API approval is now visible on GitHub.
            self.approved.add(commit_id)
        return {"id": 1, "url": "https://github.com/r/1"}

    def fake_agent_review(self, config, workspace, tier, *args):
        return ({"summary": "", "mermaid": "", "corpus_synthesis": "", "verdict": self.verdict,
                 "verdict_reason": "", "test_gaps": [], "related_tests": [], "targeted_tests": self.targeted,
                 "approve_note": "LGTM.", "coverage_notes": "", "findings": self.findings}, {"seconds": 1})

    def advance(self):
        self.pipe.advance(self.store.get_pr(self.key))
        return self.store.get_pr(self.key)

    def test_green_pr_comments_then_waits_for_formal_approval(self):
        self.advance()  # review
        card = self.advance()  # Advisory only; no formal approval
        self.assertEqual(card["stage"], "ready")
        self.assertEqual([w[0] for w in self.writes], ["COMMENT"])
        self.assertEqual(self.writes[0][1], HEAD)
        self.assertEqual(self.slack.sent, [])  # Advisory is never announced as Approved.
        self.approved.add(HEAD)  # QQ now posts a formal current-head approval on GitHub.
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        card = self.advance()
        self.assertTrue(card["decision"]["merge_ready"])
        self.advance()
        self.assertNotIn(("merge",), self.writes)  # merging is only ever your click
        self.assertEqual(len(self.slack.sent), 1)  # replies are not repeated
        self.pipe.merge(self.key)
        self.assertIn(("merge",), self.writes)
        self.assertEqual(self.store.agent_merged_keys(), set())  # your click is not an agent auto-merge


    def failing_ci(self, classification, evidence="", completed="2026-10-30T01:00:00Z"):
        failing = dict(check("gpu-test", conclusion="FAILURE"), completedAt=completed)
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit"), failing])
        name = github.summarize_checks(self.snap["statusCheckRollup"])["failures"][0]["name"]
        return {"failures": [{"name": name, "classification": classification, "evidence": evidence}],
                "summary": classification}

    def comments(self):
        return [w[1] for w in self.writes if w[0] == "comment"]

    def age_actions(self, seconds):
        self.store.conn.execute("UPDATE actions SET ts = ts - ?", (seconds,))
        self.store.conn.commit()

    def test_agent_text_is_translated_once_for_the_chinese_view(self):
        self.findings = [{"severity": "minor", "category": "style", "path": "a.py", "line": 3, "title": "Off by one",
                          "scenario": "loop skips the last item", "evidence": "", "fix": "use <=",
                          "comment": "[P2] off by one"}]
        self.advance()
        self.pipe.translator = SyncExecutor()
        calls = []

        def fake(config, texts):
            calls.append(list(texts))
            return {text: "中文：" + text for text in texts}

        with mock.patch.object(agent, "translate_zh", side_effect=fake):
            self.pipe.queue_translation()
            self.assertEqual(calls, [])  # nothing is translated until someone opens it in Chinese
            self.assertTrue(self.pipe.request_translation(self.key))
            self.pipe.queue_translation()
            self.pipe.queue_translation()
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.pipe.request_translation(self.key))  # all there now
        self.assertIn("Off by one", calls[0])
        self.assertNotIn("[P2] off by one", calls[0])  # what gets posted on GitHub stays English
        self.assertEqual(self.store.translations(["Off by one"]), {"Off by one": "中文：Off by one"})

    def test_failed_translation_leaves_the_card_alone(self):
        self.findings = [{"severity": "minor", "category": "style", "path": "a.py", "line": 3, "title": "t",
                          "scenario": "s", "evidence": "", "fix": "", "comment": "[P2] x"}]
        self.advance()
        stage = self.store.get_pr(self.key)["stage"]
        self.pipe.translator = SyncExecutor()
        with mock.patch.object(agent, "translate_zh", side_effect=agent.AgentError("boom")):
            self.pipe.request_translation(self.key)
            self.pipe.queue_translation()
        self.assertEqual(self.store.get_pr(self.key)["stage"], stage)
        self.assertIn("boom", self.pipe.status["translate_error"])
        self.assertIsNone(self.pipe.translating)

    def test_auto_merge_merges_and_tells_the_slack_thread(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"]["auto_merge"] = True
        self.advance()  # review
        self.advance()  # approve; GitHub still shows REVIEW_REQUIRED, so not mergeable yet
        self.assertNotIn(("merge",), self.writes)
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        card = self.advance()
        self.assertEqual(self.writes.count(("merge",)), 1)
        self.assertEqual((card["stage"], card["stage_reason"]), ("closed", "merged"))
        self.assertEqual([text for _, _, text in self.slack.sent][-1],
                         "[BBuf Review Agent] Merged <https://github.com/sgl-project/sglang/pull/7|sglang#7>, thanks!")
        self.assertEqual(len(self.slack.sent), 2)  # approved, then merged
        self.assertEqual(self.store.count_events("merge", 0), 1)
        self.assertEqual(self.store.agent_merged_keys(), {self.key})  # shown in violet under Merged


    def test_auto_merge_respects_hold_markers(self):
        self.approved.add(HEAD)  # Explicit formal GitHub approval, not a model verdict.
        self.config["policy"]["auto_merge"] = True
        self.snap = snapshot(labels=["run-ci", "do-not-merge"], rollup=[check("unit")])
        self.advance()
        self.advance()
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        card = self.advance()
        self.assertNotIn(("merge",), self.writes)
        self.assertTrue(card["decision"]["merge_ready"])  # left on Ready to merge for you
        self.assertIn("do-not-merge", card["stage_reason"])


    def add_event_ago(self, kind, message, seconds, key="sgl-project/sglang#1"):
        self.store.add_event(key, kind, message)
        self.store.conn.execute("UPDATE events SET ts = ts - ? WHERE id = (SELECT MAX(id) FROM events)", (seconds,))
        self.store.conn.commit()

    def test_auto_merges_are_spaced_out(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"]["auto_merge"] = True
        self.add_event_ago("merge", "you merged aaaaaaaa", 60)  # your own merge does not hold the agent back
        self.add_event_ago("merge", "agent merged bbbbbbbb", 20 * 60)
        self.advance()
        self.advance()
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        card = self.advance()
        self.assertNotIn(("merge",), self.writes)
        self.assertEqual(card["stage"], "approved")
        self.assertIn("next auto-merge slot at", card["stage_reason"])
        self.assertTrue(card["decision"]["merge_ready"])
        self.store.conn.execute("UPDATE events SET ts = ts - 3600 WHERE kind = 'merge'")
        self.store.conn.commit()
        card = self.advance()
        self.assertEqual(self.writes.count(("merge",)), 1)
        self.assertEqual(card["stage_reason"], "merged")


    def test_daily_auto_merge_cap_counts_only_agent_merges(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"].update(auto_merge=True, max_auto_merges_per_day=2)
        for hours in (3, 5):
            self.add_event_ago("merge", "agent merged cccccccc", hours * 3600)
        self.add_event_ago("merge", "you merged dddddddd", 2 * 3600)
        self.advance()
        self.advance()
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        card = self.advance()
        self.assertNotIn(("merge",), self.writes)
        self.assertIn("daily auto-merge budget used", card["stage_reason"])


    def test_failed_auto_merge_backs_off_and_stays_ready(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"]["auto_merge"] = True
        self.advance()
        self.advance()
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        calls = []

        def refuse(*args):
            calls.append(args)
            raise github.GitHubError("GraphQL: Pull request is not mergeable")

        with mock.patch.object(github, "merge", side_effect=refuse):
            card = self.advance()
            self.assertEqual(card["stage"], "approved")
            self.assertTrue(card["decision"]["merge_ready"])
            self.assertIn("auto-merge failed, retrying", card["stage_reason"])
            self.advance()
            self.assertEqual(len(calls), 1)  # backing off
            failure = card["decision"]["merge_failure"]
            self.store.update_pr(self.key, decision=dict(card["decision"], merge_failure=dict(failure, at=0)))
            self.advance()
            self.assertEqual(len(calls), 2)
            self.assertEqual(self.store.get_pr(self.key)["stage"], "approved")  # still the agent's job
            card = self.store.get_pr(self.key)
            self.store.update_pr(self.key, decision=dict(card["decision"], merge_failure=dict(
                card["decision"]["merge_failure"], at=0)))
            card = self.advance()  # third failure in a row: now it needs you
            self.assertEqual(len(calls), 3)
            self.assertEqual((card["stage"], card["decision"]["ready_kind"]), ("ready", "merge"))
            self.assertIn("auto-merge failed 3 times", card["stage_reason"])
            card = self.advance()  # backing off, still with you, no flip-flop
            self.assertEqual(len(calls), 3)
            self.assertEqual(card["stage"], "ready")
        self.assertEqual([t for _, _, t in self.slack.sent if "Merged" in t], [])


    def test_no_slack_reply_into_a_workspace_that_is_no_longer_connected(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"]["auto_merge"] = True
        self.pipe.slack_teams = lambda: {"T-OTHER"}
        self.advance()
        self.advance()
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        self.advance()
        self.assertIn(("merge",), self.writes)
        self.assertEqual(self.slack.sent, [])


    def test_merge_blocked_by_unlinked_commit_goes_to_the_author(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"]["auto_merge"] = True
        self.advance()
        self.advance()
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        blocked = github.GitHubError("Pull request is not mergeable: the base branch policy prohibits the merge. "
                                     "To use administrator privileges to immediately merge the pull request, add the `--admin` flag.")
        with mock.patch.object(github, "merge", side_effect=blocked), \
                mock.patch.object(github, "unlinked_commits", return_value=["44f5c51be55b"]):
            card = self.advance()
            self.assertEqual(card["stage"], "needs_author")
            notes = [w[1] for w in self.writes if w[0] == "comment"]
            self.assertEqual(len(notes), 1)
            self.assertIn("commit `44f5c51b` isn't linked to your GitHub account", notes[0])
            for _ in range(4):  # retries keep it with the author, never on you, one note only
                card = self.store.get_pr(self.key)
                self.store.update_pr(self.key, decision=dict(card["decision"], merge_failure=dict(
                    card["decision"]["merge_failure"], at=0)))
                card = self.advance()
            self.assertEqual(card["stage"], "needs_author")
            self.assertEqual(len([w for w in self.writes if w[0] == "comment"]), 1)
        self.snap = dict(self.snap)  # the author linked the email: the next retry merges
        card = self.store.get_pr(self.key)
        self.store.update_pr(self.key, decision=dict(card["decision"], merge_failure=dict(
            card["decision"]["merge_failure"], at=0)))
        self.assertEqual(self.advance()["stage"], "closed")


    def test_draft_is_reviewed_when_someone_asks_in_slack(self):
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit")], draft=True)
        self.advance()
        card = self.advance()
        self.assertEqual(card["review_head"], HEAD)  # reviewed despite the draft
        self.assertEqual(self.writes[0][0], "COMMENT")
        self.assertEqual(card["stage"], "ready")
        self.assertEqual(self.slack.sent, [])


    def test_your_own_merge_closes_the_slack_ask_once_for_all_its_prs(self):
        self.store.add_mention({"id": "slack:T1:C1:1.0", "source": "slack", "team_id": "T1", "channel_id": "C1",
                                "ts": 1, "status": "agent", "kind": "review",
                                "pr_keys": [self.key, "sgl-project/sglang#8"]})
        self.store.ensure_pr("sgl-project/sglang", 8, requester=self.store.get_pr(self.key)["requesters"][0])
        self.store.update_pr("sgl-project/sglang#8", url="https://github.com/sgl-project/sglang/pull/8", state="OPEN")
        self.advance()
        self.snap = dict(self.snap, state="MERGED", mergedBy={"login": "BBuf"})  # you merged it on GitHub
        self.advance()
        self.assertEqual([t for _, _, t in self.slack.sent if "Merged" in t], [])  # #8 is still open
        self.store.update_pr("sgl-project/sglang#8", state="MERGED", stage="closed", stage_reason="merged",
                             meta={"merged_by": "BBuf"})
        self.pipe.announce_recent_merges()
        self.pipe.announce_recent_merges()
        merged = [t for _, _, t in self.slack.sent if "Merged" in t]
        self.assertEqual(merged, ["[BBuf Review Agent] Merged <https://github.com/sgl-project/sglang/pull/7|sglang#7> "
                                  "and <https://github.com/sgl-project/sglang/pull/8|sglang#8>, thanks!"])

    def test_merge_by_someone_else_is_not_announced(self):
        self.advance()
        self.snap = dict(self.snap, state="MERGED", mergedBy={"login": "someone-else"})
        self.advance()
        self.pipe.announce_recent_merges()
        self.assertEqual([t for _, _, t in self.slack.sent if "Merged" in t], [])

    def test_two_asks_in_one_thread_get_one_reply(self):
        card = self.store.get_pr(self.key)
        second = dict(card["requesters"][0], id="slack:T1:C1:2.0", ts=2)  # a follow-up in the same thread
        self.store.update_pr(self.key, requesters=card["requesters"] + [second])
        self.advance()
        self.advance()
        self.assertEqual(sum(w[0] == "COMMENT" for w in self.writes), 1)
        self.assertFalse(any("Approved" in text for _, _, text in self.slack.sent))


    def test_merge_made_elsewhere_is_not_announced(self):
        self.advance()
        self.advance()
        self.snap = dict(self.snap, state="MERGED")
        card = self.advance()
        self.assertEqual(card["stage"], "closed")
        self.assertEqual([t for _, _, t in self.slack.sent if "Merged" in t], [])

    def test_advisory_is_once_per_head_and_never_announced_as_approval(self):
        self.advance()
        self.advance()  # advisory head 1
        self.snap = snapshot(head=NEW_HEAD, labels=["run-ci"], rollup=[check("unit")])
        self.advance()  # e.g. an agent fix pushed a new head: review again
        self.advance()  # advisory head 2
        self.assertEqual(sum(w[0] == "COMMENT" for w in self.writes), 2)
        self.assertNotIn("APPROVE", [w[0] for w in self.writes])
        self.assertFalse(any("Approved" in text for _, _, text in self.slack.sent))


    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_infra_failure_is_retried_then_noted_not_parked(self):
        triage = self.failing_ci("infra", "runner lost")
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="runner lost"):
            self.advance()  # review
            self.advance()  # approve right away; failure → triage job (inline)
            card = self.advance()  # first retry
            self.assertEqual(self.writes[0][0], "APPROVE")
            self.assertEqual(self.comments(), ["/rerun-failed-ci"])
            self.assertEqual((card["stage"], card["stage_reason"]),
                             ("approved", "1 CI failure(s), agent retrying: /rerun-failed-ci (1/3)"))
            self.advance()  # the rerun has not produced a new result yet: no resend
            self.assertEqual(len(self.comments()), 1)
            for _ in range(3):  # each rerun fails again → next retry, at most three
                self.failing_ci("infra", "runner lost", completed="2099-01-01T00:00:00Z")
                card = self.advance()
            self.assertEqual(self.comments(), ["/rerun-failed-ci"] * 3)
            self.age_actions(3600)
            self.snap = dict(self.snap, reviewDecision="APPROVED")
            card = self.advance()
        self.assertEqual((card["stage"], card["decision"]["merge_ready"]), ("approved", True))
        self.assertIn("kept failing for infra reasons", card["decision"]["merge_notes"][0])
        self.assertEqual(len(self.slack.sent), 1)  # only the approval; retries stay off Slack

    def test_unattributed_failure_gets_one_rerun_then_the_author(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        triage = self.failing_ci("unknown", "Segmentation fault")
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="..."):
            self.advance()
            self.advance()
            card = self.advance()
            self.assertEqual(self.comments(), ["/rerun-failed-ci"])
            self.assertEqual((card["stage"], card["stage_reason"]),
                             ("approved", "1 unattributed CI failure(s), rerunning once"))
            self.advance()
            self.assertEqual(self.comments(), ["/rerun-failed-ci"])  # waits for the rerun
            self.failing_ci("unknown", "Segmentation fault", completed="2099-01-01T00:00:00Z")
            card = self.advance()
            self.advance()
        self.assertEqual(card["stage"], "needs_author")
        notes = self.comments()[1:]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith(github.agent_review_body(HEAD, "[BBuf Review Agent] @someone these still fail after a rerun")))
        self.assertIn("Segmentation fault", notes[0])


    def test_failure_without_a_readable_log_is_rerun_then_watched_never_pinged(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        triage = self.failing_ci("unknown", "(not fetched: over the 20-log cap) No failure log is available.")
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="..."):
            self.advance()
            self.advance()
            self.advance()  # first rerun
            self.failing_ci("unknown", "(not fetched: over the 20-log cap)", completed="2099-01-01T00:00:00Z")
            card = self.advance()
            self.assertEqual(self.comments(), ["/rerun-failed-ci"])  # backing off before the next rerun
            self.assertEqual(card["stage_reason"], "1 CI failure(s) without a readable log, agent rerunning (1/3)")
            for n in (2, 3):
                self.age_actions(4 * 3600)
                card = self.advance()
                self.assertEqual(self.comments(), ["/rerun-failed-ci"] * n)
            self.assertIn("rerunning (3/3)", card["stage_reason"])
            self.age_actions(4 * 3600)
            card = self.advance()
        self.assertEqual(self.comments(), ["/rerun-failed-ci"] * 3)  # never an @author note
        self.assertEqual(card["stage"], "approved")
        self.assertIn("without a readable log; the agent keeps watching", card["stage_reason"])


    def test_quoted_log_fetch_failure_is_never_quoted_to_the_author(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        missing = "(log fetch failed: gh run view --repo: log not found: 124328582203)"
        triage = self.failing_ci("unknown", f'"{missing}"')
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="..."):
            self.advance()
            self.advance()
            self.advance()  # one rerun
            self.failing_ci("unknown", f'"{missing}"', completed="2099-01-01T00:00:00Z")
            card = self.advance()
        self.assertEqual(self.comments(), ["/rerun-failed-ci"])  # no @author note quoting the fetch error
        self.assertIn("without a readable log, agent rerunning", card["stage_reason"])


    def test_related_call_on_a_job_without_a_log_is_not_sent_to_the_author(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        triage = self.failing_ci("related", "AITER decode path changed, so this AMD job likely regressed")
        missing = "(log fetch failed: gh run view --repo: log not found: 1)"
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value=missing):
            self.advance()
            self.advance()
            card = self.advance()  # no log read: treated as unattributed, one rerun
            self.assertEqual(self.comments(), ["/rerun-failed-ci"])
            self.assertIn("rerunning once", card["stage_reason"])
            self.failing_ci("related", "AITER decode path changed", completed="2099-01-01T00:00:00Z")
            card = self.advance()
            card = self.advance()
        self.assertEqual(self.comments(), ["/rerun-failed-ci"])  # never an @author note
        self.assertEqual(card["stage"], "approved")
        self.assertEqual(self.slack.sent[-1][2].count("CI failures"), 0)


    def test_triage_reads_every_matrix_leg(self):
        legs = [dict(check(f"cpu ({i})", conclusion="FAILURE")) for i in range(3)]
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit")] + legs)
        fetched = []
        triage = {"failures": [], "summary": ""}
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", side_effect=lambda url, n: fetched.append(url) or "log"):
            self.advance()
            self.advance()
        self.assertEqual(len(fetched), 3)

    def test_related_failure_pings_the_author_with_the_error(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        triage = self.failing_ci("related", "AssertionError: expected 3 got 4")
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="..."):
            self.advance()
            self.advance()
            card = self.advance()
            self.advance()
        self.assertEqual(card["stage"], "needs_author")
        self.assertEqual(len(self.comments()), 1)
        self.assertIn("look related to this change", self.comments()[0])
        self.assertIn("AssertionError: expected 3 got 4", self.comments()[0])
        self.assertIn("CI failures look related to the change", self.slack.sent[-1][2])


    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_held_fork_runs_are_approved_on_needed_lanes_only(self):
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit", status="IN_PROGRESS", conclusion="")])
        self.held = [{"id": 11, "workflow": "PR Test", "url": "u1"},
                     {"id": 12, "workflow": "PR Test (NPU)", "url": "u2"}]
        self.advance()
        self.advance()
        self.advance()
        self.assertEqual([w for w in self.writes if w[0] == "approve-run"], [("approve-run", 11)])

    def test_held_runs_of_a_workflow_change_are_left_to_you(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        files = [{"path": ".github/workflows/pr-test.yml", "additions": 3, "deletions": 1},
                 {"path": "python/sglang/srt/managers/scheduler.py", "additions": 3, "deletions": 1}]
        self.snap = snapshot(labels=["run-ci"], files=files)
        self.held = [{"id": 11, "workflow": "PR Test", "url": "u1"}]
        self.advance()
        card = self.advance()
        self.assertEqual((card["stage"], card["decision"]["ready_kind"]), ("ready", "ci"))
        self.assertNotIn("approve-run", [w[0] for w in self.writes])


    def test_approval_budget_waits_in_the_agent_queue(self):
        self.config["policy"]["max_auto_approvals_per_day"] = 0
        self.advance()
        card = self.advance()
        self.assertEqual(card["stage"], "queued")
        self.assertIn("approval budget", card["stage_reason"])
        self.assertEqual(self.writes, [])

    def test_confirmed_blockers_go_to_the_author_even_when_unsure(self):
        self.verdict = "needs_human"
        self.findings = [{"severity": "major", "category": "correctness", "path": "a.py", "line": 3,
                          "title": "t", "scenario": "s", "evidence": "", "fix": "", "comment": "[P1] x"}]
        self.advance()
        card = self.advance()
        self.assertEqual(card["stage"], "needs_author")
        self.assertEqual(self.writes[0][0], "COMMENT")

    def test_blocking_findings_are_published_and_reported(self):
        self.verdict = "changes_needed"
        self.findings = [{"severity": "blocker", "category": "correctness", "path": "a.py", "line": 3,
                          "title": "t", "scenario": "s", "evidence": "", "fix": "", "comment": "[P1] x"}]
        self.advance()
        card = self.advance()
        self.assertEqual(card["stage"], "needs_author")
        self.assertEqual(self.writes[0][0], "COMMENT")
        self.assertIn("1 thing(s) to fix, see the PR comments: https://github.com/r/1", self.slack.sent[-1][2])
        self.advance()
        self.assertEqual(sum(w[0] == "COMMENT" for w in self.writes), 1)

    def test_unresolved_earlier_blocker_is_kept_but_not_posted_twice(self):
        self.verdict = "changes_needed"
        self.findings = [{"severity": "blocker", "category": "correctness", "path": "a.py", "line": 3,
                          "title": "Prove the fused path runs", "scenario": "s", "evidence": "raised by BBuf earlier",
                          "fix": "", "comment": "[P1] prove it", "raised_before": True}]
        self.advance()
        card = self.advance()
        self.assertEqual(card["stage"], "needs_author")  # still a blocker, not approved
        event, _, body, inline = self.writes[0]
        self.assertEqual((event, inline), ("COMMENT", []))  # the inline P1 is not repeated
        self.assertIn("Still open from my last review:\n- Prove the fused path runs (`a.py:3`)", body)
        self.assertEqual(body.splitlines()[0], github.AGENT_REVIEW_NOTICE)
        self.assertIn(HEAD, body)
        self.assertIn("One thing to fix before this can go in.", body)


    def blocker(self):
        self.verdict = "changes_needed"
        self.findings = [{"severity": "blocker", "category": "correctness", "path": "a.py", "line": 3,
                          "title": "Empty batch crashes", "scenario": "s", "evidence": "", "fix": "guard it",
                          "comment": "[P1] x"}]

    def test_agent_fixes_findings_instead_of_waiting_on_the_author(self):
        self.config["policy"]["auto_fix"] = True
        self.blocker()
        runs = []
        self.pipe.run_fix = lambda card, snap, kind, issues: runs.append((kind, issues)) or {
            "pushed": True, "head": "f" * 40, "summary": "guard the empty batch", "fixed": ["F1"], "skipped": []}
        self.advance()
        card = self.advance()
        self.assertEqual([(kind, [i["title"] for i in issues]) for kind, issues in runs],
                         [("findings", ["Empty batch crashes"])])
        self.assertEqual(card["stage"], "queued")
        self.assertNotIn("COMMENT", [w[0] for w in self.writes])  # no findings review: fixed instead
        note = self.comments()[-1]
        self.assertTrue(note.startswith(github.agent_review_body(HEAD, "[BBuf Review Agent] @someone I pushed `ffffffff` to your branch: Guard the empty batch. Please pull before pushing again.")))
        self.advance()
        self.assertEqual(len(runs), 1)  # one attempt per head

    def test_unfixable_findings_still_go_to_the_author(self):
        self.config["policy"]["auto_fix"] = True
        self.blocker()
        self.pipe.run_fix = lambda *a: {"pushed": False, "reason": "needs a design decision"}
        self.advance()
        self.assertEqual(self.advance()["stage"], "queued")  # attempt recorded
        card = self.advance()
        self.assertEqual(card["stage"], "needs_author")
        self.assertEqual(self.writes[0][0], "COMMENT")  # the findings are posted for the author

    def author_replies(self, decision, reply="Fair point, the later commits only merge main; dropping this."):
        self.config["policy"]["auto_discuss"] = True
        self.blocker()
        self.advance()
        self.assertEqual(self.advance()["stage"], "needs_author")  # findings posted
        self.discussion = [
            {"id": 1, "kind": "review", "author": "BBuf", "body": "[P1] x", "url": "u1", "in_reply_to": None,
             "path": "a.py", "line": 3, "created_at": "2026-10-30T10:00:00Z"},
            {"id": 2, "kind": "review", "author": "someone", "body": "@BBuf is this needed?", "url": "u2",
             "in_reply_to": 1, "path": "a.py", "line": 3, "created_at": "2026-10-30T11:00:00Z"}]
        answer = {"answers": [{"comment_id": "2", "decision": decision, "about": "finding",
                               "finding": "Empty batch crashes", "reply": reply}], "summary": ""}
        calls = []
        with mock.patch.object(agent, "discuss", side_effect=lambda *a: calls.append(a) or (answer, {})):
            self.advance()  # answers the reply
            card = self.advance()
            self.advance()
        self.assertEqual(len(calls), 1)  # answered once
        self.assertEqual([w for w in self.writes if w[0] == "reply"], [("reply", 1, github.agent_review_body(HEAD, "[BBuf Review Agent] " + reply))])
        return card

    def test_author_is_right_so_the_finding_is_dropped_and_commented(self):
        card = self.author_replies("concede")
        self.assertEqual(card["decision"]["waived_findings"], ["Empty batch crashes"])
        self.assertNotIn("APPROVE", [w[0] for w in self.writes])
        self.assertTrue(any(a["event"] == "review:agent-comment" for a in self.store.actions(self.key)))


    def test_finding_that_still_holds_is_explained_once(self):
        card = self.author_replies("hold", "It still needs the empty-batch guard at a.py:3.")
        self.assertEqual(card["stage"], "needs_author")
        self.assertNotIn("APPROVE", [w[0] for w in self.writes])

    def test_a_real_judgment_call_goes_to_you(self):
        card = self.author_replies("maintainer", "This is a trade-off for the maintainer to accept.")
        self.assertEqual((card["stage"], card["decision"]["ready_kind"]), ("ready", "look"))
        self.assertIn("the author disputes a finding", card["stage_reason"])

    def test_asking_for_more_evidence_never_blocks(self):
        self.verdict = "changes_needed"
        self.findings = [{"severity": "blocker", "category": "performance-evidence", "path": "a.py", "line": 3,
                          "title": "Re-measure on this head", "scenario": "s", "evidence": "", "fix": "",
                          "comment": "[P1] rerun the benchmark"},
                         {"severity": "major", "category": "x", "kind": "evidence", "path": "a.py", "line": 4,
                          "title": "Add GPU coverage", "scenario": "s", "evidence": "", "fix": "", "comment": "[P1] test"}]
        self.advance()
        card = self.advance()
        self.assertEqual(card["review"]["verdict"], "minor")
        self.assertEqual([w[0] for w in self.writes if w[0] in ("APPROVE", "COMMENT")], ["COMMENT"])  # nothing posted


    def test_a_repeated_evidence_request_is_dropped(self):
        self.findings = [{"severity": "minor", "category": "x", "kind": "evidence", "path": "a.py", "line": 3,
                          "title": "Re-measure again", "scenario": "s", "evidence": "asked before", "fix": "",
                          "comment": "Non-blocking: rerun", "raised_before": True},
                         {"severity": "minor", "category": "x", "kind": "evidence", "path": "a.py", "line": 4,
                          "title": "One accuracy check", "scenario": "s", "evidence": "", "fix": "",
                          "comment": "Non-blocking: gsm8k", "raised_before": False}]
        card = self.advance()
        self.assertEqual([f["title"] for f in card["review"]["findings"]], ["One accuracy check"])
        self.assertEqual(card["review"]["dropped"][-1]["verify_reason"], "repeats an earlier evidence request")

    def maintainer_says(self, body, decision, kind="review"):
        self.config["policy"]["auto_discuss"] = True
        self.blocker()
        self.advance()
        self.assertEqual(self.advance()["stage"], "needs_author")
        self.store.update_pr(self.key, first_seen=0)
        root = {"id": 1, "kind": "review", "author": "BBuf", "body": "[P1] x", "url": "u1", "in_reply_to": None,
                "path": "a.py", "line": 3, "created_at": "2026-10-30T10:00:00Z"}
        mine = {"id": 3, "kind": kind, "author": "BBuf", "body": body, "url": "u3",
                "in_reply_to": 1 if kind == "review" else None, "path": "a.py", "line": 3,
                "created_at": "2026-10-30T12:00:00Z"}
        self.discussion = [root, mine]
        answer = {"answers": [{"comment_id": "3", "decision": decision, "about": "finding",
                               "finding": "Empty batch crashes", "reply": ""}], "summary": ""}
        calls = []
        with mock.patch.object(agent, "discuss", side_effect=lambda *a: calls.append(a) or (answer, {})):
            self.advance()
            card = self.advance()
            self.advance()
        self.assertEqual(len(calls), 1)
        self.assertEqual([w for w in self.writes if w[0] == "reply"], [])  # you are never answered
        self.assertTrue(calls[0][3][0]["from_maintainer"])
        return self.store.get_pr(self.key)

    def test_your_ignore_this_drops_the_finding_and_the_pr_moves_on(self):
        card = self.maintainer_says("Yes, just ignore this", "concede")
        self.assertIn("Empty batch crashes", card["decision"]["waived_findings"])
        self.assertNotIn("APPROVE", [w[0] for w in self.writes])
        self.assertTrue(any(a["event"] == "review:agent-comment" for a in self.store.actions(self.key)))


    def test_model_interpretation_of_your_lgtm_only_comments(self):
        card = self.maintainer_says("LGTM, the guard is fine", "approve", kind="issue")
        self.assertNotIn("APPROVE", [w[0] for w in self.writes])
        self.assertTrue(any(a["event"] == "review:agent-comment" for a in self.store.actions(self.key)))
        self.assertEqual(card["stage"], "needs_author")  # A model-read LGTM cannot clear confirmed risks.



    def test_your_change_request_waits_on_the_author(self):
        card = self.maintainer_says("Please also guard the decode path", "hold")
        self.assertEqual(card["decision"]["waiting_author_head"], HEAD)
        self.assertEqual(card["stage"], "needs_author")

    def test_a_thanks_is_read_but_not_answered(self):
        self.config["policy"]["auto_discuss"] = True
        self.advance()
        self.discussion = [
            {"id": 5, "kind": "issue", "author": "BBuf", "body": "/tag-and-rerun-ci", "url": "u", "in_reply_to": None,
             "created_at": "2026-10-30T10:00:00Z"},
            {"id": 6, "kind": "issue", "author": "someone", "body": "thanks!", "url": "u6", "in_reply_to": None,
             "created_at": "2026-10-30T11:00:00Z"}]
        answer = {"answers": [{"comment_id": "6", "decision": "none", "about": "other", "finding": "", "reply": ""}],
                  "summary": ""}
        calls = []
        with mock.patch.object(agent, "discuss", side_effect=lambda *a: calls.append(a) or (answer, {})):
            self.advance()
            self.advance()
            self.advance()
        self.assertEqual(len(calls), 1)
        self.assertNotIn("comment", [w[0] for w in self.writes if "thanks" in str(w)])
        self.assertEqual([w for w in self.writes if w[0] == "reply"], [])

    def test_ci_failure_the_author_explains_stops_blocking(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"]["auto_discuss"] = True
        triage = self.failing_ci("related", "AssertionError")
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="..."):
            self.advance()
            self.advance()
            self.assertEqual(self.advance()["stage"], "needs_author")  # author pinged about the CI failure
            self.discussion = [
                {"id": 7, "kind": "issue", "author": "BBuf", "body": "@someone these CI failures look related",
                 "url": "u", "in_reply_to": None, "created_at": "2026-10-30T10:00:00Z"},
                {"id": 8, "kind": "issue", "author": "someone", "body": "gpu-test is broken on main too, see #1",
                 "url": "u8", "in_reply_to": None, "created_at": "2026-10-30T11:00:00Z"}]
            answer = {"answers": [{"comment_id": "8", "decision": "concede", "about": "ci", "finding": "gpu-test",
                                   "reply": "You're right, it fails on main as well."}], "summary": ""}
            self.snap = dict(self.snap, reviewDecision="APPROVED")
            with mock.patch.object(agent, "discuss", return_value=(answer, {})):
                self.advance()
                card = self.advance()
        self.assertEqual(card["decision"]["ci_waived"], ["gpu-test"])
        self.assertEqual((card["stage"], card["decision"]["merge_ready"]), ("approved", True))


    def test_replies_are_read_while_the_pr_waits_at_a_gate(self):
        self.config["policy"]["auto_discuss"] = True
        self.snap = snapshot(labels=["run-ci"], body="", title="[Perf] faster thing")  # perf PR without numbers
        self.advance()
        self.discussion = [
            {"id": 9, "kind": "issue", "author": "BBuf", "body": "To move this forward, please: ...", "url": "u",
             "in_reply_to": None, "created_at": "2026-10-30T10:00:00Z"},
            {"id": 10, "kind": "issue", "author": "someone", "body": "which numbers do you need exactly?",
             "url": "u10", "in_reply_to": None, "created_at": "2026-10-30T11:00:00Z"}]
        answer = {"answers": [{"comment_id": "10", "decision": "hold", "about": "other", "finding": "",
                               "reply": "Before/after tok/s on the same GPU and command."}], "summary": ""}
        with mock.patch.object(agent, "discuss", return_value=(answer, {})):
            self.advance()
        self.assertIn(github.agent_review_body(HEAD, "[BBuf Review Agent] @someone Before/after tok/s on the same GPU and command."), self.comments())

    def test_your_hand_off_and_the_per_pr_cap_stop_agent_fixes(self):
        self.config["policy"]["auto_fix"] = True
        self.blocker()
        runs = []
        self.pipe.run_fix = lambda *a: runs.append(a) or {"pushed": False, "reason": "x"}
        self.advance()
        card = self.store.get_pr(self.key)
        self.store.update_pr(self.key, decision=dict(card["decision"], waiting_author_head=HEAD))
        self.advance()
        self.assertEqual(runs, [])  # you handed this head to the author
        self.store.update_pr(self.key, decision=dict(card["decision"], waiting_author_head=""))
        for i in range(2):
            self.store.claim_action(self.key, f"old{i}", "fix:findings")
            self.store.finish_action(self.key, f"old{i}", "fix:findings", True, {"pushed": True})
        self.advance()
        self.assertEqual(runs, [])  # two agent pushes on this PR already

    def test_agent_fixes_run_one_at_a_time_and_pushes_are_spaced_out(self):
        self.config["policy"]["auto_fix"] = True
        self.blocker()
        runs = []
        self.pipe.run_fix = lambda card, snap, kind, issues: runs.append(kind) or {
            "pushed": True, "head": "f" * 40, "summary": "guard the empty batch", "fixed": ["F1"], "skipped": []}
        self.advance()
        self.pipe.inflight["sgl-project/sglang#2"] = "job_fix"
        card = self.advance()
        self.assertEqual(runs, [])
        self.assertEqual(card["stage"], "queued")  # waiting for its slot, not handed to the author
        self.assertTrue(card["stage_reason"].endswith("next fix slot after the running fix"))
        self.assertNotIn("COMMENT", [w[0] for w in self.writes])
        del self.pipe.inflight["sgl-project/sglang#2"]
        self.add_event_ago("fix", "agent could not fix the CI failures caused by this change: x", 60)
        self.add_event_ago("fix", "agent pushed 12345678: something", 5 * 60)
        card = self.advance()
        self.assertEqual(runs, [])
        self.assertIn("next fix slot", card["stage_reason"])
        self.store.conn.execute("UPDATE events SET ts = ts - 900 WHERE kind = 'fix'")
        self.store.conn.commit()
        self.advance()
        self.assertEqual(runs, ["findings"])

    def test_running_fixes_count_against_the_daily_fix_cap(self):
        self.config["policy"].update(auto_fix=True, max_agent_fixes_per_day=1)
        self.blocker()
        self.pipe.run_fix = lambda *a: self.fail("over the daily cap")
        self.advance()
        self.pipe.inflight["sgl-project/sglang#2"] = "job_fix"
        card = self.advance()
        self.assertEqual(card["stage"], "needs_author")  # cap used by the running fix: the author is asked
        self.assertEqual(self.writes[0][0], "COMMENT")

    def test_branch_closed_to_maintainer_edits_goes_to_the_author_without_using_a_slot(self):
        self.config["policy"]["auto_fix"] = True
        self.push_target = None
        self.blocker()
        self.pipe.run_fix = lambda *a: self.fail("cannot push to this branch")
        self.advance()
        card = self.advance()
        self.assertEqual(card["stage"], "needs_author")
        self.assertEqual(self.writes[0][0], "COMMENT")
        self.assertEqual(self.store.count_events("fix", 0), 0)
        self.assertTrue(any("maintainer edits are off" in e["message"] for e in self.store.events(self.key)))
        self.advance()
        self.assertEqual(sum(w[0] == "COMMENT" for w in self.writes), 1)

    # ---- agent budget: windows, token cap, follow-up reviews, CI rules --------
    def close_window(self):
        later = (datetime.now() + timedelta(hours=3)).strftime("%H:%M")
        self.config["agent"].update(windows=[later], window_minutes=60)
        return later

    def counting_review(self):
        calls = []
        original = self.fake_agent_review
        return calls, lambda *a: calls.append(a) or original(*a)

    def test_agent_waits_for_its_window_unless_you_asked(self):
        later = self.close_window()
        calls, fake = self.counting_review()
        with mock.patch.object(agent, "review", side_effect=fake):
            card = self.advance()
            self.assertEqual(calls, [])
            self.assertEqual((card["stage"], card["stage_reason"]),
                             ("queued", f"waiting for the next agent window ({later})"))
            self.store.update_pr(self.key, decision=dict(card["decision"], run_now_until=time.time() + 600))
            self.advance()  # you clicked something on this card: it runs now
        self.assertEqual(len(calls), 1)

    def test_daily_token_budget_is_a_hard_stop(self):
        self.config["agent"].update(daily_token_budget=1_000_000, token_reserve=150_000)
        agent.usage_hook("review", 900_000)  # every codex run reports here
        self.assertEqual(self.store.tokens_since(self.pipe.day_start()), 900_000)
        calls, fake = self.counting_review()
        with mock.patch.object(agent, "review", side_effect=fake):
            card = self.advance()
        self.assertEqual(calls, [])
        self.assertTrue(card["stage_reason"].startswith("today's agent token budget is used (0.9M of 1M)"))
        self.store.update_pr(self.key, decision=dict(card["decision"], run_now_until=time.time() + 600))
        with mock.patch.object(agent, "review", side_effect=fake):
            self.advance()
        self.assertEqual(calls, [])  # asking for it skips the window, never the budget

    def test_running_jobs_hold_part_of_the_budget(self):
        self.config["agent"].update(daily_token_budget=400_000, token_reserve=150_000)
        self.assertEqual(self.pipe.agent_gate(), "")
        self.pipe.inflight["x#1"] = "job_review"
        self.assertEqual(self.pipe.agent_gate(), "")
        self.pipe.inflight["x#2"] = "job_fix"
        self.assertIn("token budget is used", self.pipe.agent_gate())

    def test_run_now_opens_a_window_by_hand(self):
        self.close_window()
        self.config["agent"]["window_minutes"] = 60
        calls, fake = self.counting_review()
        with mock.patch.object(agent, "review", side_effect=fake):
            self.assertIn("waiting for the next agent window", self.advance()["stage_reason"])
            self.pipe.run_now()
            self.assertGreater(self.pipe.agent_status()["manual_until"], time.time() + 3000)
            self.advance()
            self.assertEqual(len(calls), 1)
            self.pipe.stop_run()
            self.assertEqual(self.pipe.agent_status()["manual_until"], 0)
            self.assertFalse(self.pipe.window_state()[0])

    def test_daily_limit_is_set_from_the_page_and_kept(self):
        self.config["agent"].update(daily_token_budget=5_000_000, token_reserve=150_000)
        agent.usage_hook("review", 4_900_000)
        status = self.pipe.agent_status()
        self.assertTrue(status["exhausted"])
        self.assertEqual((status["budget"], status["default_budget"]), (5_000_000, 5_000_000))
        with self.assertRaises(ValueError):  # a run by hand cannot get past the cap
            self.pipe.run_now()
        self.pipe.set_token_budget(10_000_000)
        self.assertFalse(self.pipe.agent_status()["exhausted"])
        self.assertEqual(Pipeline(self.config, self.store).token_budget(), 10_000_000)  # survives a restart
        self.pipe.run_now()

    def test_window_state(self):
        self.config["agent"].update(windows=["10:00", "15:00", "21:00"], window_minutes=120)
        day = datetime(2026, 10, 31)
        at = lambda h, m: day.replace(hour=h, minute=m).timestamp()  # noqa: E731
        self.assertEqual(self.pipe.window_state(at(11, 30)), (True, at(15, 0)))
        self.assertEqual(self.pipe.window_state(at(12, 30)), (False, at(15, 0)))
        self.assertEqual(self.pipe.window_state(at(23, 30)), (False, (day + timedelta(days=1)).replace(hour=10).timestamp()))
        self.assertEqual(self.pipe.window_state(at(22, 59)), (True, (day + timedelta(days=1)).replace(hour=10).timestamp()))

    def reviewed_then_pushed(self, published=False):
        self.verdict = "changes_needed"
        self.findings = [{"severity": "blocker", "category": "correctness", "path": "a.py", "line": 3,
                          "title": "Empty batch crashes", "scenario": "s", "evidence": "", "fix": "guard it",
                          "comment": "[P1] x", "kind": "bug"}]
        self.advance()
        if published:
            self.assertEqual(self.advance()["stage"], "needs_author")  # findings posted on the PR
        card = self.store.get_pr(self.key)
        self.store.update_pr(self.key, review=dict(card["review"], diff_fp="fp-1"))
        self.snap = snapshot(head=NEW_HEAD, labels=["run-ci"], rollup=[check("unit")])

    def test_head_that_only_merged_main_keeps_its_review(self):
        self.reviewed_then_pushed(published=True)
        calls, fake = self.counting_review()
        with mock.patch.object(FakeWorkspace, "diff_fingerprint", create=True, new=lambda ws: "fp-1"), \
                mock.patch.object(agent, "review", side_effect=fake):
            card = self.advance()
        self.assertEqual(calls, [])
        self.assertEqual(card["review_head"], NEW_HEAD)
        self.assertEqual(card["review"]["findings"][0]["title"], "Empty batch crashes")
        self.assertTrue(card["review"]["findings"][0]["raised_before"])  # posted already: not posted again
        self.assertEqual(self.store.count_events("review", 0), 1)  # a carry-over is not a review run
        card = self.advance()
        self.assertEqual(sum(w[0] == "COMMENT" for w in self.writes), 1)  # nothing new: no second review posted
        self.assertEqual((card["stage"], card["stage_reason"]),
                         ("needs_author", "blocking issues confirmed, already posted, waiting for the author"))
        self.assertIn("only main was merged in", " ".join(e["message"] for e in self.store.events(self.key, 20)))

    def test_new_head_gets_a_follow_up_review_of_the_delta_only(self):
        self.reviewed_then_pushed(published=True)
        delta_calls, full_calls, verified = [], [], []

        def fake_delta(config, workspace, tier, skill_dir, since, previous):
            delta_calls.append((since, [f["title"] for f in previous]))
            return ({"summary": "- guards the batch", "mermaid": "", "corpus_synthesis": "", "verdict": "changes_needed",
                     "verdict_reason": "", "test_gaps": [], "related_tests": [], "targeted_tests": [],
                     "approve_note": "", "coverage_notes": "",
                     "findings": [dict(self.findings[0], line=5),
                                  {"severity": "major", "category": "correctness", "path": "a.py", "line": 9,
                                   "title": "New off-by-one", "scenario": "s", "evidence": "", "fix": "",
                                   "comment": "x", "kind": "bug", "raised_before": False}]}, {"seconds": 1})

        def fake_verify(config, workspace, findings):
            verified.append([f["title"] for f in findings])
            return {"verdicts": [{"index": i, "verdict": "confirmed", "reason": "ok"} for i in range(len(findings))]}, {}

        with mock.patch.object(FakeWorkspace, "diff_fingerprint", create=True, new=lambda ws: "fp-2"), \
                mock.patch.object(FakeWorkspace, "prepare_delta", create=True, new=lambda ws, since: "+guard\n"), \
                mock.patch.object(agent, "review_delta", side_effect=fake_delta), \
                mock.patch.object(agent, "verify", side_effect=fake_verify), \
                mock.patch.object(agent, "review", side_effect=lambda *a: full_calls.append(a)):
            card = self.advance()
        self.assertEqual(full_calls, [])
        self.assertEqual(delta_calls, [(HEAD, ["Empty batch crashes"])])
        self.assertEqual(verified, [["New off-by-one"]])  # the carried finding keeps its verification
        titles = {f["title"]: f for f in card["review"]["findings"]}
        self.assertEqual(titles["Empty batch crashes"]["verify"], "confirmed")
        self.assertTrue(titles["Empty batch crashes"]["raised_before"])  # already posted: not posted again
        self.assertEqual((card["review_head"], card["review"]["delta_since"]), (NEW_HEAD, HEAD))
        self.assertIn("Since aaaaaaaa:", card["review"]["summary"])

    def test_empty_delta_keeps_the_review_without_the_agent(self):
        self.reviewed_then_pushed()
        with mock.patch.object(FakeWorkspace, "diff_fingerprint", create=True, new=lambda ws: "fp-2"), \
                mock.patch.object(FakeWorkspace, "prepare_delta", create=True, new=lambda ws, since: ""), \
                mock.patch.object(agent, "review_delta", side_effect=AssertionError("no agent")), \
                mock.patch.object(agent, "review", side_effect=AssertionError("no agent")):
            card = self.advance()
        self.assertEqual(card["review_head"], NEW_HEAD)

    def test_rereview_is_a_full_review(self):
        self.reviewed_then_pushed()
        card = self.store.get_pr(self.key)
        self.store.update_pr(self.key, review_head="", decision=dict(card["decision"], full_review=True))
        calls, fake = self.counting_review()
        with mock.patch.object(FakeWorkspace, "diff_fingerprint", create=True, new=lambda ws: "fp-1"), \
                mock.patch.object(FakeWorkspace, "prepare_delta", create=True, new=lambda ws, since: "+x\n"), \
                mock.patch.object(agent, "review_delta", side_effect=AssertionError("full review wanted")), \
                mock.patch.object(agent, "review", side_effect=fake):
            card = self.advance()
        self.assertEqual(len(calls), 1)
        self.assertFalse(card["decision"].get("full_review"))

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_setup_step_failures_are_classified_without_the_agent(self):
        self.failed_step = "Run ./.github/actions/check-pr-test-health"
        self.failing_ci("unknown")
        with mock.patch.object(agent, "triage_ci", side_effect=AssertionError("rules should decide")), \
                mock.patch.object(github, "failed_job_log", return_value="Health check failed"):
            self.advance()
            card = self.advance()
            card = self.advance()
        triage = card["ci"]["triage"]
        self.assertEqual(([f["classification"] for f in triage["failures"]], triage["pending"]), (["infra"], []))
        self.assertIn("/rerun-failed-ci", self.comments())

    def test_failures_rules_cannot_read_wait_for_the_agent_window(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.failing_ci("unknown")
        later = self.close_window()
        card = self.store.get_pr(self.key)
        self.store.update_pr(self.key, decision=dict(card["decision"], run_now_until=time.time() + 600))
        calls = []
        triage = {"failures": [{"name": self.failing_ci("unknown")["failures"][0]["name"],
                                "classification": "related", "evidence": "AssertionError: 3 != 4"}], "summary": "s"}
        with mock.patch.object(agent, "triage_ci", side_effect=lambda *a: calls.append(a) or (triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="E   AssertionError: 3 != 4"):
            self.advance()  # reviewed now (you asked), approved
            self.store.update_pr(self.key, decision=dict(self.store.get_pr(self.key)["decision"], run_now_until=0))
            card = self.advance()  # CI failure: rules leave it, the agent waits for its window
            self.assertEqual(calls, [])
            self.assertEqual(card["ci"]["triage"]["pending"], [triage["failures"][0]["name"]])
            card = self.advance()
            self.assertEqual(self.comments(), ["/rerun-failed-ci"])  # one fresh run meanwhile, free
            self.failing_ci("unknown", completed="2099-01-01T00:00:00Z")
            card = self.advance()
            self.assertEqual(card["stage_reason"],
                             f"1 CI failure(s) still failing after a rerun; waiting for the next agent window ({later})")
            self.config["agent"]["windows"] = []  # the window opens
            self.advance()
            card = self.advance()
        self.assertEqual(len(calls), 1)
        self.assertEqual(card["stage"], "needs_author")


    def test_agent_fix_waits_for_the_window_instead_of_going_to_the_author(self):
        self.config["policy"]["auto_fix"] = True
        self.blocker()
        self.pipe.run_fix = lambda *a: self.fail("outside the window")
        self.advance()  # reviewed (window still open)
        later = self.close_window()
        card = self.advance()
        self.assertEqual((card["stage"], card["stage_reason"]),
                         ("queued", f"agent will fix the verified review findings; waiting for the next agent window ({later})"))
        self.assertNotIn("COMMENT", [w[0] for w in self.writes])

    def test_agent_resolves_conflicts_on_an_approved_pr(self):
        self.approved.add(HEAD)  # Formal approval already posted on GitHub, separate from the agent.
        self.config["policy"]["auto_fix"] = True
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit")], mergeable="CONFLICTING")
        runs = []
        self.pipe.run_fix = lambda card, snap, kind, issues: runs.append(kind) or {
            "pushed": True, "head": "e" * 40, "summary": "merged main and resolved the conflicts in `a.py`",
            "fixed": [], "skipped": []}
        self.advance()
        card = self.advance()
        self.assertNotIn("APPROVE", [w[0] for w in self.writes])
        self.assertEqual(runs, ["conflict"])
        self.assertEqual(card["stage"], "queued")
        self.assertNotIn("rebase onto main", "\n".join(self.comments()))



    def test_head_you_already_approved_is_not_approved_again(self):
        self.approved = {HEAD}
        self.advance()
        card = self.advance()
        self.assertEqual(card["stage"], "approved")
        self.assertEqual([w for w in self.writes if w[0] == "APPROVE"], [])

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_t3_is_left_for_you(self):
        files = [{"path": "scripts/clip.py", "additions": 500, "deletions": 20}]  # jj-agent: export engine is the hot path
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit")], files=files)
        self.advance()
        card = self.advance()
        self.assertEqual((card["stage"], card["decision"]["ready_kind"]), ("ready", "look"))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.slack.sent, [])  # a T3 hand-off to you is not an outcome

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_hand_off_to_author_holds_until_a_new_push(self):
        files = [{"path": "scripts/clip.py", "additions": 500, "deletions": 20}]  # jj-agent: export engine is the hot path
        self.snap = snapshot(labels=["run-ci"], rollup=[check("unit")], files=files)
        self.advance()
        card = self.advance()
        self.assertEqual(card["stage"], "ready")  # T3: yours
        self.store.update_pr(self.key, decision=dict(card["decision"], waiting_author_head=HEAD))
        self.assertEqual(self.advance()["stage"], "needs_author")
        self.assertEqual(self.advance()["stage"], "needs_author")  # refreshes do not pull it back
        self.snap = dict(self.snap, headRefOid=NEW_HEAD)
        self.advance()  # the author pushed: review again
        self.assertEqual(self.advance()["stage"], "ready")

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_targeted_tests_gate_the_merge_not_the_approval(self):
        self.targeted = ["test_y.py"]
        self.advance()
        card = self.advance()  # approves on the code alone, then sends /rerun-test
        self.assertEqual(self.writes[0][0], "APPROVE")
        self.assertTrue(self.writes[1][1].startswith("/rerun-test test_y.py"))
        self.assertEqual((card["stage"], card["stage_reason"]), ("approved", "CI passed, targeted tests running"))
        self.rerun = {"state": "running", "url": "https://github.com/c/2", "runs": []}
        self.assertFalse(self.advance()["decision"]["merge_ready"])
        self.rerun = {"state": "passed", "url": "https://github.com/c/2", "runs": []}
        self.snap = dict(self.snap, reviewDecision="APPROVED")
        card = self.advance()
        self.assertEqual((card["stage"], card["decision"]["merge_ready"]), ("approved", True))
        self.assertEqual(len(self.comments()), 1)

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_targeted_failure_related_to_the_change_pings_the_author(self):
        self.targeted = ["test_y.py"]
        self.advance()
        self.advance()
        self.rerun = {"state": "failed", "url": "https://github.com/c/2", "updated_at": "2099-01-01T00:00:00Z",
                      "runs": ["https://github.com/sgl-project/sglang/actions/runs/5"]}
        self.targeted_jobs = [{"name": "test_y", "conclusion": "FAILURE", "url": "https://j/1", "completed_at": ""}]
        triage = {"failures": [{"name": "targeted / test_y", "classification": "related",
                                "evidence": "mismatch in logits"}], "summary": "related"}
        with mock.patch.object(agent, "triage_ci", return_value=(triage, {})), \
                mock.patch.object(github, "failed_job_log", return_value="..."):
            self.advance()
            card = self.advance()
        self.assertEqual(card["stage"], "needs_author")
        self.assertIn("[`test_y`](https://j/1): `mismatch in logits`", self.comments()[-1])

    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_unanswered_rerun_test_is_sent_again_later(self):
        self.targeted = ["test_y.py"]
        self.advance()
        self.advance()
        self.advance()
        self.assertEqual(len(self.comments()), 1)
        self.age_actions(1900)
        card = self.advance()
        self.assertEqual(len(self.comments()), 2)
        self.assertEqual(card["stage"], "approved")

    def test_conflict_does_not_block_advisory_but_formal_approval_is_separate(self):
        self.snap = snapshot(labels=["run-ci"], mergeable="CONFLICTING")
        self.advance()  # reviewed despite the conflict
        card = self.advance()
        self.assertEqual(self.writes[0][0], "COMMENT")
        self.assertEqual(card["stage"], "ready")
        self.approved.add(HEAD)  # Formal human approval precedes merge/CI judgment.
        card = self.advance()
        notes = [w[1] for w in self.writes if w[0] == "comment"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith(github.agent_review_body(HEAD, "[BBuf Review Agent] Could you rebase this onto the latest main?")))
        self.assertEqual((card["stage"], card["stage_reason"]), ("needs_author", "approved; merge conflict with main"))
        self.assertFalse(card["decision"]["merge_ready"])
        self.advance()
        self.assertEqual(len([w for w in self.writes if w[0] == "comment"]), 1)  # one reminder per head
        self.writes.clear()
        self.store.update_pr(self.key, requesters=[{"id": "github-mention:x", "source": "github-mention"}])
        self.snap = snapshot(head=NEW_HEAD, labels=["run-ci"], draft=True)
        self.assertEqual(self.advance()["stage"], "needs_author")
        self.assertEqual(self.writes, [])  # drafts nobody asked about in Slack are never nagged


    @unittest.skip("jj-agent overlay: SGLang CI slash commands (/tag-and-rerun-ci, /rerun-test), run-ci label and hardware lanes do not exist in jj-agent; see tests/test_jj_agent_adapter.py")
    def test_gate_bounce_tells_author_once(self):
        self.snap = snapshot(labels=["run-ci"], body="")
        self.advance()
        self.advance()
        notes = [w for w in self.writes if w[0] == "comment"]
        self.assertEqual(len(notes), 1)
        self.assertIn("add the test commands and results", notes[0][1])
        self.assertIn("before review, please add the test commands", self.slack.sent[-1][2])


class LogPlaceholderTest(unittest.TestCase):
    def test_placeholders_are_recognised_even_when_quoted(self):
        for text in ["(log fetch failed: gh run view --repo: log not found: 1)",
                     '"(log fetch failed: gh run view --repo: log not found: 1)"',
                     "(not fetched: over the 20-log cap)", "(empty log)", "(log too large, not downloaded within 300 s)",
                     "(not a GitHub Actions job, no log)"]:
            self.assertTrue(github.log_missing(text), text)
            self.assertEqual(evidence_line({"evidence": text}), "", text)
        self.assertFalse(github.log_missing("E   AssertionError: expected 3 got 4"))
        self.assertEqual(evidence_line({"evidence": "`AssertionError: expected 3 got 4`"}),
                         "AssertionError: expected 3 got 4")


class CommentStyleTest(unittest.TestCase):
    def finding(self, severity, comment, kind="bug"):
        return {"severity": severity, "kind": kind, "category": "correctness", "comment": comment}

    def test_level_markers_come_from_severity_not_the_model(self):
        self.assertEqual(finding_comment(self.finding("blocker", "[P1] Empty batches crash here. Could we guard it?")),
                         "Empty batches crash here. Could we guard it?")
        self.assertEqual(finding_comment(self.finding("minor", "[P2] could this live in the base class?")),
                         "Minor: Could this live in the base class?")
        self.assertEqual(finding_comment(self.finding("nit", "Nit: typo in the name")), "nit: Typo in the name")
        self.assertEqual(finding_comment(self.finding("minor", "Could we add a B200 run?", kind="evidence")),
                         "Non-blocking: Could we add a B200 run?")

    def test_em_dashes_are_rewritten(self):
        self.assertEqual(plain("This breaks TP=7 — the layer expects 480 features."),
                         "This breaks TP=7, the layer expects 480 features.")


class TokenMeterTest(unittest.TestCase):
    def test_tokens_used_reads_the_codex_footer(self):
        self.assertEqual(agent.tokens_used("...\ncodex\n{}\ntokens used\n21,365\n"), 21365)
        self.assertEqual(agent.tokens_used("no footer"), 0)

    def test_fingerprint_ignores_where_main_is(self):
        a = "diff --git a/x b/x\nindex 111..222 100644\n@@ -10,3 +10,4 @@ def f():\n+    return 1\n"
        b = "diff --git a/x b/x\nindex 333..444 100644\n@@ -40,3 +41,4 @@ def f():\n+    return 1\n"
        c = "diff --git a/x b/x\nindex 333..444 100644\n@@ -40,3 +41,4 @@ def f():\n+    return 2\n"
        self.assertEqual(agent.fingerprint(a), agent.fingerprint(b))
        self.assertNotEqual(agent.fingerprint(a), agent.fingerprint(c))


class CIRulesTest(unittest.TestCase):
    def test_rules(self):
        failures = [
            {"name": "PR Test / pr-test-finish", "log_tail": "x"},
            {"name": "PR Test / gpu (1)", "log_tail": "x", "failed_step": "Set up job"},
            {"name": "PR Test / gpu (2)", "log_tail": "(log fetch failed: log not found: 1)"},
            {"name": "PR Test / gpu (3)", "log_tail": "a\nOSError: [Errno 28] No space left on device\nb"},
            {"name": "PR Test / gpu (4)", "log_tail": "requests.exceptions.ConnectionError: Connection reset by peer"},
            {"name": "PR Test / gpu (5)", "log_tail": "AssertionError: 3 != 4", "failed_step": "Run test"},
        ]
        ruled = {r["name"].split(" / ")[-1]: r["classification"] for r in ci.rule_triage(failures)}
        self.assertEqual(ruled, {"pr-test-finish": "unrelated", "gpu (1)": "infra", "gpu (2)": "unclassified",
                                 "gpu (3)": "infra"})  # a reset connection may be a crashed server: the agent decides


class SlackIntakeTest(unittest.TestCase):
    def test_only_review_requests_with_pr_links_become_cards(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(agent, "usage_hook", None):
            config = config_module.load(None)
            config.update(data_dir=tmp, notify_macos=False)
            store = Store(Path(tmp) / "t.sqlite3")
            with contextlib.closing(store.conn):
                pipe = Pipeline(config, store)
                now = time.time()
                t = [f"{now - 3600 + i:.6f}" for i in range(6)]
                old = f"{now - 20 * 86400:.6f}"  # past the 14-day window
                fake = FakeSlack(matches=[
                    {"ts": t[0], "user": "U2", "username": "alice", "channel": {"id": "C1", "name": "dev"},
                     "text": "<@U1> could you review <https://github.com/sgl-project/sglang/pull/11|#11>?",
                     "permalink": "https://x.slack.com/archives/C1/p11"},
                    {"ts": t[1], "user": "U3", "username": "bob", "channel": {"id": "C1", "name": "dev"},
                     "text": "<@U1> FYI https://github.com/sgl-project/sglang/pull/12 landed",
                     "permalink": "https://x.slack.com/archives/C1/p12"},
                    {"ts": t[2], "user": "U3", "username": "bob2", "channel": {"id": "C1", "name": "dev"},
                     "text": "<@U1> 帮忙看下这个", "permalink": f"https://x.slack.com/archives/C1/p13?thread_ts={t[5]}",
                     "parent": "https://github.com/sgl-project/sglang/pull/13"},
                    {"ts": t[3], "user": "U4", "username": "carol", "channel": {"id": "C2", "name": "random"},
                     "text": "<@U1> lunch?", "permalink": "https://x.slack.com/archives/C2/p14"},
                    # the parent asks for review, but this reply only says thanks
                    {"ts": t[4], "user": "U5", "username": "erin", "channel": {"id": "C1", "name": "dev"},
                     "text": "Thanks for the update! cc <@U1>",
                     "permalink": f"https://x.slack.com/archives/C1/p15?thread_ts={t[5]}"},
                    {"ts": old, "user": "U6", "username": "frank", "channel": {"id": "C1", "name": "dev"},
                     "text": "<@U1> please review https://github.com/sgl-project/sglang/pull/16",
                     "permalink": "https://x.slack.com/archives/C1/p16"},
                    {"ts": t[0], "user": "U7", "username": "jira", "bot_id": "B1", "channel": {"id": "C1", "name": "dev"},
                     "text": "", "permalink": "https://x.slack.com/archives/C1/p17"},
                ])
                fake.parents = {t[5]: "please review https://github.com/sgl-project/sglang/pull/13"}
                pipe.slack_clients = lambda: [fake]
                pipe.poll_slack(config["sources"]["slack"])
                self.assertEqual(sorted(c["number"] for c in store.list_prs()), [11, 13])
                kinds = {m["author"]: m["kind"] for m in store.recent_mentions(since=0)}
                self.assertEqual(kinds, {"alice": "review", "bob": "fyi", "bob2": "review", "carol": "other",
                                         "erin": "pr-other", "frank": "review-old"})
                card13 = store.get_pr("sgl-project/sglang#13")
                self.assertEqual(card13["requesters"][0]["thread_ts"], t[5])
                pipe.poll_slack(config["sources"]["slack"])  # idempotent
                self.assertEqual(len(store.recent_mentions(since=0)), 6)
                self.assertEqual(fake.sent, [])  # nothing is posted while ingesting


    def test_rerun_test_status_parsing(self):
        # the bot's real format: one line per runner, status emoji inside, `:done` once finished
        comments = [{"body": "/rerun-test x", "html_url": "a"},
                    {"body": "🚀 `1-gpu-h100` (1 test): ✅ [View workflow run](https://github.com/o/r/actions/runs/5) <!--rrt:0:done-->\n"
                             "```\ncd test/ && python3 x\n```\n\n"
                             "🚀 `b200` (1 test): 🔄 [View workflow run](https://github.com/o/r/actions/runs/6) <!--rrt:1-->",
                     "html_url": "b"}]
        with mock.patch.object(github, "gh_json", return_value=comments):
            status = github.rerun_test_status("o/r", 1, "2026-10-30T00:00:00Z")
        self.assertEqual((status["state"], status["url"]), ("running", "b"))
        self.assertEqual(len(status["runs"]), 2)
        body = comments[1]["body"]
        comments[1]["body"] = body.replace("🔄", "❌").replace("<!--rrt:1-->", "<!--rrt:1:done-->")
        with mock.patch.object(github, "gh_json", return_value=comments):
            self.assertEqual(github.rerun_test_status("o/r", 1, "x")["state"], "failed")
        comments[1]["body"] = body.replace("🔄", "✅").replace("<!--rrt:1-->", "<!--rrt:1:done-->")
        with mock.patch.object(github, "gh_json", return_value=comments):
            self.assertEqual(github.rerun_test_status("o/r", 1, "x")["state"], "passed")


class SlackInboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = config_module.load(None)
        self.config.update(data_dir=self.tmp.name, notify_macos=False)
        self.store = Store(Path(self.tmp.name) / "t.sqlite3")
        self.addCleanup(self.store.conn.close)
        usage = mock.patch.object(agent, "usage_hook", None)
        usage.start()
        self.addCleanup(usage.stop)
        self.pipe = Pipeline(self.config, self.store)
        now = time.time()
        self.t = [f"{now - 3600 + i:.6f}" for i in range(10)]
        t = self.t
        self.slack = FakeSlack(
            matches=[
                {"ts": t[0], "user": "U4", "username": "carol", "channel": {"id": "C2", "name": "random"},
                 "text": "<@U1> can we sync tomorrow?", "permalink": "https://x.slack.com/archives/C2/p0"},
                {"ts": t[1], "user": "U3", "username": "bob", "channel": {"id": "C1", "name": "dev"},
                 "text": "<@U1> FYI https://github.com/sgl-project/sglang/pull/12 landed",
                 "permalink": "https://x.slack.com/archives/C1/p1"},
                {"ts": t[2], "user": "U5", "username": "dan", "channel": {"id": "C3", "name": "infra"},
                 "text": "<@U1> where is the runbook?", "permalink": "https://x.slack.com/archives/C3/p2"},
            ],
            threads={("C2", t[0]): [{"user": "U4", "ts": t[0]}, {"user": "U1", "ts": t[3]}],
                     ("C1", t[1]): [{"user": "U3", "ts": t[1], "reactions": [{"name": "eyes", "users": ["U1"]}]}],
                     ("C3", t[2]): [{"user": "U5", "ts": t[2]}]},
            dms=[{"id": "D1", "is_im": True}, {"id": "D2", "is_im": True}, {"id": "D3", "is_im": True},
                 {"id": "G1", "is_mpim": True, "name": "mpdm-a--b--c"}, {"id": "G2", "is_mpim": True}],
            histories={
                "G1": [{"user": "U2", "ts": t[6], "text": "thanks all!"}],  # chatter, not to you
                "G2": [{"user": "U2", "ts": t[6], "text": "<@U1> can you confirm the date?"}],
                "D1": [{"user": "U2", "ts": t[5], "text": "ping"}, {"user": "U2", "ts": t[4], "text": "hi"},
                       {"user": "U1", "ts": t[3], "text": "yo"}],
                "D2": [{"user": "U1", "ts": t[6]}, {"user": "U3", "ts": t[5], "text": "thanks"}],
                "D3": [{"user": "U3", "ts": t[7],
                        "text": "please review https://github.com/sgl-project/sglang/pull/21"}],
            })
        self.slack.matches.append(
            {"ts": t[8], "user": "U6", "username": "gina", "channel": {"id": "C4", "name": "kernels"},
             "text": "<@U1> heads up, https://github.com/sgl-project/sglang/pull/30 touches your kernel",
             "permalink": "https://x.slack.com/archives/C4/p8"})
        self.slack.threads[("C4", t[8])] = [{"user": "U6", "ts": t[8]}]
        self.pr_state = "OPEN"
        patch = mock.patch.object(github, "gh_json", side_effect=lambda args, **kw: {"state": self.pr_state})
        patch.start()
        self.addCleanup(patch.stop)
        self.pipe.slack_clients = lambda: [self.slack]



    def test_mention_about_a_merged_pr_is_resolved(self):
        self.pipe.poll_slack(self.config["sources"]["slack"])
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        status = lambda: {m["author"]: m["status"] for m in self.store.recent_mentions(since=0)}["gina"]
        self.assertEqual(status(), "pending")  # PR still open: you still owe a reply
        self.pr_state = "MERGED"
        self.pipe._pr_states.clear()
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        self.assertEqual(status(), "resolved")

    def test_unhandled_mentions_and_dms(self):
        self.pipe.poll_slack(self.config["sources"]["slack"])
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        status = {m["author"]: m["status"] for m in self.store.recent_mentions(since=0)}
        self.assertEqual(status["carol"], "replied")  # you answered in the thread
        self.assertEqual(status["bob"], "reacted")  # you reacted 👀
        self.assertEqual(status["dan"], "pending")  # still owed
        dm = self.store.get_mention("slackdm:T1:D1")
        self.assertEqual((dm["status"], dm["count"], dm["text"]), ("pending", 2, "ping"))
        self.assertFalse(self.store.has_mention("slackdm:T1:D2"))  # your word was last
        self.assertFalse(self.store.has_mention("slackdm:T1:G1"))  # group chatter is not owed
        self.assertEqual(self.store.get_mention("slackdm:T1:G2")["status"], "pending")
        self.assertEqual(self.store.get_mention("slackdm:T1:D3")["status"], "agent")
        card = self.store.get_pr("sgl-project/sglang#21")
        self.assertEqual(card["requesters"][0]["thread_ts"], self.t[7])
        self.assertEqual(self.slack.sent, [])  # the inbox never answers anything

    def test_every_pr_link_in_a_dm_is_a_review_ask(self):
        t = self.t
        link = "https://github.com/sgl-project/sglang/pull/"
        self.slack.histories.update({
            # your reply is the latest word, but both earlier links are still asks
            "D2": [{"user": "U1", "ts": t[9], "text": "on it"},
                   {"user": "U3", "ts": t[8], "text": link + "41"},
                   {"user": "U3", "ts": t[7], "text": "and this one " + link + "42"},
                   {"user": "U3", "ts": t[6], "text": "fyi " + link + "43 landed"}],
            "G1": [{"user": "U2", "ts": t[6], "text": "merged " + link + "44"}],  # group chatter: not an ask
        })
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        cards = {c["number"]: c for c in self.store.list_prs()}
        self.assertTrue({41, 42} <= set(cards))
        self.assertNotIn(43, cards)
        self.assertNotIn(44, cards)
        self.assertEqual(cards[41]["requesters"][0]["thread_ts"], t[8])  # the reply goes under that message
        self.assertEqual(cards[42]["requesters"][0]["channel"], "DM")

    def test_watched_channel_takes_unclaimed_prs_only(self):
        t = self.t
        link = "https://github.com/sgl-project/sglang/pull/"
        self.config["sources"]["slack"]["watch_channels"] = ["pull-requests"]
        post = lambda ts, user, text: {"ts": ts, "user": user, "username": user, "text": text,  # noqa: E731
                                       "channel": {"id": "C9", "name": "pull-requests"},
                                       "permalink": "https://x.slack.com/archives/C9/p" + ts}
        self.slack.channel_matches = {"pull-requests": [
            post(t[1], "U3", "high priority " + link + "31"),
            post(t[2], "U3", "<@U7> can you take a look " + link + "32"),  # asked someone else
            post(t[3], "U3", link + "33"),  # someone else already reviews it on GitHub
            post(t[4], "U1", link + "34"),  # your own post
        ]}
        self.pipe.reviewed_by_others = lambda repo, number: number == 33
        self.pipe.poll_slack(self.config["sources"]["slack"])
        numbers = {c["number"] for c in self.store.list_prs()}
        self.assertIn(31, numbers)
        self.assertFalse({32, 33, 34} & numbers)
        card = self.store.get_pr("sgl-project/sglang#31")
        self.assertEqual((card["requesters"][0]["channel_id"], card["requesters"][0]["thread_ts"]), ("C9", t[1]))
        statuses = {m["id"].split(":")[-1]: m["status"] for m in self.store.recent_mentions(since=0)
                    if m["channel_id"] == "C9"}
        self.assertEqual((statuses[t[1]], statuses[t[2]], statuses[t[3]]), ("agent", "ignored", "ignored"))
        self.pipe.poll_slack(self.config["sources"]["slack"])  # seen posts are not re-processed
        self.assertEqual(len(self.store.list_prs()), len(numbers))

    def test_dms_found_by_search_and_threads_and_acks(self):
        t = self.t
        link = "https://github.com/sgl-project/sglang/pull/"
        # D9 is far down Slack's priority list: only search finds it
        self.slack.to_me = [{"ts": t[8], "channel": {"id": "D9", "is_im": True}}]
        self.slack.histories["D9"] = [
            {"user": "U1", "ts": t[7], "text": "can you share the numbers?", "reply_count": 2, "latest_reply": t[9]},
            {"user": "U3", "ts": t[2], "text": "thanks!"}]
        self.slack.threads[("D9", t[7])] = [
            {"user": "U1", "ts": t[7], "thread_ts": t[7]},
            {"user": "U3", "ts": t[8], "thread_ts": t[7], "text": "and could you look at " + link + "61?"},
            {"user": "U3", "ts": t[9], "thread_ts": t[7], "text": "Sure, will do"}]
        self.slack.histories["D2"] = [{"user": "U3", "ts": t[6], "text": "thanks a lot!"},
                                      {"user": "U1", "ts": t[5], "text": "merged"}]
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        dm = self.store.get_mention("slackdm:T1:D9")
        self.assertEqual(dm["status"], "agent")  # the thread reply asked for a review
        card = self.store.get_pr("sgl-project/sglang#61")
        self.assertEqual(card["requesters"][0]["thread_ts"], t[7])  # reply goes into that thread
        self.assertFalse(self.store.has_mention("slackdm:T1:D2"))  # "thanks a lot!" is not owed

    def test_review_ask_later_in_a_thread_that_mentioned_you(self):
        t = self.t
        self.pipe.poll_slack(self.config["sources"]["slack"])  # bob's FYI in C1 thread t[1] is stored
        self.slack.threads[("C1", t[1])] = [
            {"user": "U3", "ts": t[1]},
            {"user": "U7", "ts": t[4], "text": "seen it too"},
            {"user": "U7", "ts": t[5], "text": "https://github.com/sgl-project/sglang/pull/71 if someone could review"},
            {"user": "U7", "ts": t[6], "text": "<@U9> could you review https://github.com/sgl-project/sglang/pull/72"}]
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        numbers = {c["number"] for c in self.store.list_prs()}
        self.assertIn(71, numbers)
        self.assertNotIn(72, numbers)  # asked someone else by name
        self.assertEqual(self.store.get_pr("sgl-project/sglang#71")["requesters"][0]["thread_ts"], t[1])

    def test_missed_asks_are_promoted_when_their_pr_is_still_open(self):
        t = self.t
        for i, (text, number) in enumerate([
                ("Hi @BBuf, I've addressed your nit on #81. Could you merge it when you have a moment?", 81),
                ("再麻煩有空時辛苦幫忙review一下:pray: github.com/sgl-project/sglang/pull/82", 82),
                ("@BBuf FYI the fix for #83 landed", 83)]):
            self.store.add_mention({"id": f"slack:T1:C5:{t[i]}", "source": "slack", "team_id": "T1", "channel_id": "C5",
                                    "channel": "dev", "author": "zoe", "text": text, "ts": float(t[i]),
                                    "thread_ts": t[i], "kind": "pr-other", "status": "pending",
                                    "pr_keys": [f"sgl-project/sglang#{number}"]})
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        numbers = {c["number"] for c in self.store.list_prs()}
        self.assertTrue({81, 82} <= numbers)
        self.assertNotIn(83, numbers)  # a heads-up, not an ask

    def test_dismissed_dm_reopens_on_new_message(self):
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        self.store.set_mention_status("slackdm:T1:D1", "dismissed")
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        self.assertEqual(self.store.get_mention("slackdm:T1:D1")["status"], "dismissed")
        self.slack.histories["D1"].insert(0, {"user": "U2", "ts": self.t[9], "text": "again"})
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        self.assertEqual(self.store.get_mention("slackdm:T1:D1")["status"], "pending")
        self.slack.histories["D1"].insert(0, {"user": "U1", "ts": f"{time.time():.6f}", "text": "done"})
        self.pipe.poll_slack_inbox(self.config["sources"]["slack_inbox"])
        self.assertEqual(self.store.get_mention("slackdm:T1:D1")["status"], "replied")


class FakeSlack:
    def __init__(self, matches=(), threads=None, dms=None, histories=None):
        self.matches, self.sent, self.team_id, self.token = list(matches), [], "T1", "xoxp-test"
        self.threads, self.dms, self.histories = threads or {}, dms or [], histories or {}

    def identity(self):
        return "U1"

    def render(self, text):
        return text

    def user_name(self, user_id):
        return {"U2": "alice", "U3": "bob"}.get(user_id, user_id)

    def permalink(self, channel, ts, thread_ts=None):
        return f"https://x.slack.com/archives/{channel}/p{ts}"

    def replies(self, channel, ts, limit=100):
        return list(self.threads.get((channel, ts), []))

    def history(self, channel, oldest, limit=20):
        return list(self.histories.get(channel, []))

    def dm_conversations(self, limit):
        return self.dms

    channel_matches = {}

    to_me = []

    def search(self, query, lookback_days):
        if query.startswith("in:#"):
            return list(self.channel_matches.get(query[4:], []))
        if query == "to:me":
            return list(self.to_me)
        return [{k: v for k, v in m.items() if k != "parent"} for m in self.matches]

    parents = {}

    def thread_parent_text(self, channel, thread_ts):
        return self.parents.get(thread_ts, "")

    def reply(self, channel, thread_ts, text):
        self.sent.append((channel, thread_ts, text))


class CodexAccountTest(unittest.TestCase):
    def setUp(self):
        self.config = config_module.load(None)
        self.config["agent"]["codex_homes"] = ["/h/personal", "/h/work"]
        agent._resting.clear()
        self.addCleanup(agent._resting.clear)
        agent._emails.update({str(Path("/h/personal")): "me@gmail.com", str(Path("/h/work")): "me@work.ai"})

    def run_with(self, outcomes):
        calls = []

        def fake_run(cmd, **kw):
            home = Path(kw["env"]["CODEX_HOME"]).as_posix()
            calls.append(home)
            code, err = outcomes[home]
            return subprocess.CompletedProcess(cmd, code, "", err)

        with mock.patch.object(agent, "_run_tree", side_effect=fake_run):
            proc, account = agent.run_codex(self.config, ["codex"], "p", "/tmp", "review")
        return calls, proc, account

    def test_first_account_is_used_while_it_has_quota(self):
        calls, proc, account = self.run_with({"/h/personal": (0, ""), "/h/work": (0, "")})
        self.assertEqual((calls, account), (["/h/personal"], "me@gmail.com"))

    def test_spend_cap_moves_to_the_next_account_and_rests_the_first(self):
        capped = (1, "ERROR: You hit your spend cap set by the owner of your workspace.")
        calls, proc, account = self.run_with({"/h/personal": capped, "/h/work": (0, "")})
        self.assertEqual((calls, account, proc.returncode), (["/h/personal", "/h/work"], "me@work.ai", 0))
        calls, _, _ = self.run_with({"/h/personal": (0, ""), "/h/work": (0, "")})
        self.assertEqual(calls, ["/h/work"])  # the capped account rests for a while

    def test_other_errors_do_not_switch_accounts(self):
        calls, proc, _ = self.run_with({"/h/personal": (1, "model not found"), "/h/work": (0, "")})
        self.assertEqual((calls, proc.returncode), (["/h/personal"], 1))


class FixerGitTest(unittest.TestCase):
    """The git half of agent fixes against real local repositories."""

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@x", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.remote, seed, self.work = root / "remote.git", root / "seed", root / "work"
        subprocess.run(["git", "init", "--bare", "-b", "main", str(self.remote)], check=True, capture_output=True)
        subprocess.run(["git", "clone", str(self.remote), str(seed)], check=True, capture_output=True)
        (seed / "a.py").write_text("x = 1\n")
        self.git(seed, "add", "a.py")
        self.git(seed, "commit", "-m", "base")
        self.git(seed, "push", "origin", "HEAD:main")
        self.git(seed, "checkout", "-b", "feature")
        (seed / "a.py").write_text("x = 2\n")
        self.git(seed, "commit", "-am", "pr change")
        self.git(seed, "push", "origin", "feature")
        self.git(seed, "checkout", "main")
        (seed / "a.py").write_text("x = 3\n")
        self.git(seed, "commit", "-am", "main change")
        self.git(seed, "push", "origin", "main")
        self.seed = seed
        subprocess.run(["git", "clone", str(self.remote), str(self.work)], check=True, capture_output=True)
        self.git(self.work, "checkout", "--detach", "origin/feature")
        self.checkout = fixer.Checkout(self.work, "BBuf", "1+BBuf@users.noreply.github.com")

    def test_merge_resolve_and_fast_forward_push(self):
        conflicted = self.checkout.merge("origin/main")
        self.assertEqual(conflicted, ["a.py"])
        self.assertEqual(self.checkout.unresolved(conflicted), ["a.py"])
        (self.work / "a.py").write_text("x = 3  # main\ny = 2  # pr\n")  # what the agent would do
        self.assertEqual(self.checkout.unresolved(conflicted), [])
        self.checkout.finish_merge(conflicted)
        self.checkout.push(str(self.remote), "feature")
        self.assertEqual(self.git(self.remote, "rev-parse", "feature"), self.checkout.head())
        self.assertIn("1+BBuf@users.noreply.github.com", self.git(self.remote, "log", "-1", "--format=%ae", "feature"))

    def test_commit_subject_is_cut_at_a_word(self):
        long = ("Preserve fatal device-error precedence in control fan-out replies and add regressions "
                "verifying later calls are rejected")
        subject, _, body = fixer.commit_message(long).partition("\n\n")
        self.assertEqual(subject, "Preserve fatal device-error precedence in control fan-out replies")
        self.assertEqual(body, long + ".")
        self.assertEqual(fixer.commit_message("Guard the empty batch."), "Guard the empty batch")

    def test_push_never_overwrites_a_newer_author_push(self):
        (self.work / "a.py").write_text("x = 2\nz = 0\n")
        self.checkout.commit("Guard the empty batch", self.checkout.changed_files())
        self.git(self.seed, "checkout", "feature")
        (self.seed / "b.py").write_text("w = 1\n")
        self.git(self.seed, "add", "b.py")
        self.git(self.seed, "commit", "-m", "author keeps working")
        self.git(self.seed, "push", "origin", "feature")
        author_head = self.git(self.remote, "rev-parse", "feature")
        with self.assertRaises(fixer.FixError):
            self.checkout.push(str(self.remote), "feature")
        self.assertEqual(self.git(self.remote, "rev-parse", "feature"), author_head)


class WorkspaceTest(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_prepare_rebuilds_interrupted_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            origin = Path(tmp) / "origin"
            origin.mkdir()
            self.git(origin, "init", "-q", "-b", "main")
            self.git(origin, "config", "uploadpack.allowFilter", "true")
            (origin / "a.py").write_text("x = 1\n")
            self.git(origin, "add", ".")
            self.git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
            (origin / "a.py").write_text("x = 2\n")
            self.git(origin, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "pr")
            head = self.git(origin, "rev-parse", "HEAD")
            self.git(origin, "update-ref", "refs/pull/5/head", head)
            self.git(origin, "reset", "-q", "--hard", "HEAD~1")
            config = {"data_dir": tmp, "git_url": f"file://{origin}"}
            snap = snapshot(head=head)
            with mock.patch.object(github, "gh_json", return_value=[]):
                ws = agent.Workspace(config, "o/r", 5, head, "main")
                ws.prepare(snap)
                self.assertIn("+x = 2", (ws.ctx / "diff.patch").read_text())
                (ws.worktree / "a.py").unlink()  # simulate an interrupted checkout
                ws.prepare(snap)
                self.assertEqual((ws.worktree / "a.py").read_text(), "x = 2\n")
                ws.cleanup()
                self.assertFalse(ws.worktree.exists())


class SyncExecutor:
    def submit(self, fn, *args):
        fn(*args)


class FakeWorkspace:
    def __init__(self, config, repo, number, head, base_ref):
        self.ctx = Path(config["data_dir"])
        self.head = head

    def prepare(self, snap):
        (self.ctx / "diff.patch").write_text("")

    def cleanup(self):
        pass



class RunTreeTimeoutTest(unittest.TestCase):
    """A timed-out agent must not leave its helper processes running."""

    def alive(self, pid):
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
            return str(pid) in out
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def test_timeout_kills_grandchildren(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "grandchild.pid"
            child = ("import subprocess,sys,time;"
                     "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);"
                     f"open({str(pidfile)!r},'w').write(str(p.pid));time.sleep(120)")
            with self.assertRaises(subprocess.TimeoutExpired):
                agent._run_tree([sys.executable, "-c", child], input="", text=True, timeout=5)
            grandchild = int(pidfile.read_text())
            deadline = time.time() + 10
            while self.alive(grandchild) and time.time() < deadline:
                time.sleep(0.2)
            self.assertFalse(self.alive(grandchild))

    def test_normal_run_returns_output(self):
        proc = agent._run_tree([sys.executable, "-c", "import sys;print(sys.stdin.read().upper())"],
                               input="ok", text=True, timeout=30)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "OK"))


if __name__ == "__main__":
    unittest.main()


class TailnetHostTest(unittest.TestCase):
    """The dashboard answers on the tailnet name only for listed tailnet users."""

    def ok(self, headers, tailnet):
        import server
        h = server.Handler.__new__(server.Handler)
        h.headers = headers
        h.server = SimpleNamespace(tailnet=tailnet)
        return h._host_ok()

    def test_rules(self):
        tn = {"host": "box.tail1.ts.net", "logins": {"me@example.com"}}
        self.assertTrue(self.ok({"Host": "127.0.0.1:8775"}, None))
        self.assertTrue(self.ok({"Host": "box.tail1.ts.net", "Tailscale-User-Login": "Me@Example.com"}, tn))
        self.assertFalse(self.ok({"Host": "box.tail1.ts.net"}, tn))  # tagged device: no login header
        self.assertFalse(self.ok({"Host": "box.tail1.ts.net", "Tailscale-User-Login": "other@example.com"}, tn))
        self.assertFalse(self.ok({"Host": "evil.example", "Tailscale-User-Login": "me@example.com"}, tn))
        self.assertFalse(self.ok({"Host": "box.tail1.ts.net", "Tailscale-User-Login": "me@example.com"}, None))

