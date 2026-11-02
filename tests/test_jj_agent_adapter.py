"""Tests for the jj-agent overlay of sglang-review-inbox (inbox/triage.py,
inbox/ci.py, inbox/skill.py, prompts/). Run from the adapted checkout:

    python3 -m unittest discover -s tests
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from inbox import ci, skill  # noqa: E402
from inbox.config import DEFAULTS  # noqa: E402
from inbox.triage import triage  # noqa: E402

POLICY = DEFAULTS["policy"]


def snapshot(files=(), body="unittest: Ran 10 tests OK", title="x", draft=False, rollup=(), state="OPEN"):
    return {"state": state, "isDraft": draft, "mergeable": "MERGEABLE", "title": title, "body": body,
            "files": list(files), "additions": sum(f.get("additions", 0) for f in files),
            "deletions": sum(f.get("deletions", 0) for f in files), "headRefOid": "abc123",
            "labels": [], "statusCheckRollup": list(rollup), "commits": []}


def check(name, status="COMPLETED", conclusion="SUCCESS", workflow="tests"):
    return {"__typename": "CheckRun", "workflowName": workflow, "name": name, "status": status,
            "conclusion": conclusion, "detailsUrl": "https://github.com/x/y/actions/runs/1/job/1"}


class TriageJJAgentTest(unittest.TestCase):
    def test_server_change_is_t3(self):
        files = [{"path": "scripts/app_server.py", "additions": 5, "deletions": 1}]
        result = triage(snapshot(files=files), POLICY)
        self.assertEqual((result["risk_level"], result["tier"]), ("high", "T3"))

    def test_packaging_change_is_t3(self):
        files = [{"path": "scripts/build_package.py", "additions": 20, "deletions": 2}]
        self.assertEqual(triage(snapshot(files=files), POLICY)["tier"], "T3")

    def test_network_files_are_t3_even_when_small(self):
        files = [{"path": "scripts/ai_editor.py", "additions": 3, "deletions": 0}]
        self.assertEqual(triage(snapshot(files=files), POLICY)["tier"], "T3")

    def test_small_friendly_text_change_is_t1(self):
        files = [{"path": "scripts/friendly.py", "additions": 4, "deletions": 1}]
        self.assertEqual(triage(snapshot(files=files), POLICY)["tier"], "T1")

    def test_clip_engine_change_is_t2_or_more(self):
        files = [{"path": "scripts/clip.py", "additions": 120, "deletions": 10},
                 {"path": "tests/test_clip.py", "additions": 40, "deletions": 0}]
        result = triage(snapshot(files=files, body="Ran 544 tests OK; 音画同步 每个接缝 <= 19 ms"), POLICY)
        self.assertEqual(result["tier"], "T2")
        self.assertEqual([g["id"] for g in result["gates"]], [])

    def test_export_change_without_sync_readings_is_gated(self):
        files = [{"path": "scripts/project.py", "additions": 30, "deletions": 0},
                 {"path": "tests/test_export_space.py", "additions": 90, "deletions": 0}]
        result = triage(snapshot(files=files, body="Ran 603 tests ... OK (skipped=32)"), POLICY)
        gates = {g["id"]: g for g in result["gates"]}
        self.assertIn("avsync", gates)
        self.assertTrue(gates["avsync"]["tell_author"])
        self.assertIn("40 ms", gates["avsync"]["ask"])

    def test_cleanroom_gate_is_for_a_human_not_the_author(self):
        files = [{"path": "reference/capcut-investigation/NOTES.md", "additions": 1, "deletions": 0}]
        result = triage(snapshot(files=files), POLICY)
        gate = next(g for g in result["gates"] if g["id"] == "cleanroom")
        self.assertEqual((gate["kind"], gate["tell_author"]), ("human", False))
        self.assertEqual(result["tier"], "T3")
        cited = triage(snapshot(files=[{"path": "README.md", "additions": 1, "deletions": 0}],
                                body="see reference/capcut-investigation/STATUS.md"), POLICY)
        self.assertIn("cleanroom", [g["id"] for g in cited["gates"]])

    def test_docs_only_is_t1_without_test_plan_gate(self):
        files = [{"path": "VERIFICATION.md", "additions": 10, "deletions": 2}]
        result = triage(snapshot(files=files, body="docs"), POLICY)
        self.assertEqual((result["tier"], result["gates"]), ("T1", []))

    def test_test_plan_gate_accepts_chinese_meiPao(self):
        files = [{"path": "scripts/cleanup.py", "additions": 10, "deletions": 2}]
        gated = triage(snapshot(files=files, body="改了口头禅规则"), POLICY)
        self.assertIn("tests", [g["id"] for g in gated["gates"]])
        ok = triage(snapshot(files=files, body="全量 Ran 603 tests OK；ruff 没跑"), POLICY)
        self.assertNotIn("tests", [g["id"] for g in ok["gates"]])


class CIJJAgentTest(unittest.TestCase):
    REQUIRED = "linux (ubuntu-24.04)"
    OPTIONAL = "linux (ubuntu-latest)"

    def test_required_leg_green_optional_leg_red_is_a_pass_with_a_note(self):
        rollup = [check(self.REQUIRED), check(self.OPTIONAL, conclusion="FAILURE")]
        result = ci.assess({"review": {}}, snapshot(rollup=rollup))
        self.assertEqual((result["status"], result["passed"]), ("green", True))
        self.assertEqual(result["suggestions"], [])
        self.assertTrue(any("ubuntu-latest" in n for n in result["notes"]))

    def test_required_leg_red_needs_triage_and_blocks_when_related(self):
        rollup = [check(self.REQUIRED, conclusion="FAILURE"), check(self.OPTIONAL)]
        snap = snapshot(rollup=rollup)
        first = ci.assess({"review": {}}, snap)
        self.assertEqual(first["status"], "failed")
        self.assertTrue(first["needs_triage"])
        self.assertFalse(first["passed"])
        name = first["failures"][0]["name"]
        record = {"signature": ci.signature(first["failures"]),
                  "failures": [{"name": name, "classification": "related", "evidence": "AssertionError"}]}
        second = ci.assess({"review": {}}, snap, record)
        self.assertEqual([f["name"] for f in second["blocking"]], [name])
        self.assertFalse(second["passed"])

    def test_infra_failure_is_never_auto_rerun_only_noted(self):
        rollup = [check(self.REQUIRED, conclusion="FAILURE")]
        snap = snapshot(rollup=rollup)
        first = ci.assess({"review": {}}, snap)
        name = first["failures"][0]["name"]
        record = {"signature": ci.signature(first["failures"]),
                  "failures": [{"name": name, "classification": "infra", "evidence": "Could not resolve host"}]}
        result = ci.assess({"review": {}}, snap, record)
        self.assertEqual(result["suggestions"], [])
        self.assertEqual(len(result["retryable"]), 1)
        self.assertFalse(result["passed"])
        self.assertTrue(any("gh run rerun" in n for n in result["notes"]))

    def test_pending_and_cancelled_are_not_passes(self):
        running = ci.assess({"review": {}}, snapshot(rollup=[check(self.REQUIRED, status="IN_PROGRESS", conclusion=None)]))
        self.assertEqual((running["status"], running["passed"]), ("running", False))
        cancelled = ci.assess({"review": {}}, snapshot(rollup=[check(self.REQUIRED, conclusion="CANCELLED")]))
        self.assertEqual((cancelled["status"], cancelled["passed"]), ("cancelled", False))
        nothing = ci.assess({"review": {}}, snapshot(rollup=[]))
        self.assertEqual((nothing["status"], nothing["passed"]), ("not_started", False))

    def test_no_platform_lanes(self):
        self.assertEqual(ci.pr_platforms(snapshot(title="[amd] rocm")), set())
        self.assertIsNone(ci.lane_platform("tests / linux (ubuntu-24.04)"))

    def test_rules_classify_setup_and_network_errors(self):
        failures = [
            {"name": "tests / linux (ubuntu-24.04)", "log_tail": "x", "failed_step": "System tools (FFmpeg with H.264, a Chinese font for the subtitle tests)"},
            {"name": "tests / linux (ubuntu-24.04)", "log_tail": "E: Unable to fetch some archives\nW: ..."},
            {"name": "tests / linux (ubuntu-24.04)", "log_tail": "FAIL: test_cut_export ... AssertionError: 77 != 0", "failed_step": "Tests"},
        ]
        ruled = [r["classification"] for r in ci.rule_triage(failures)]
        self.assertEqual(ruled, ["infra", "infra"])  # the AssertionError is left to the agent


class SkillJJAgentTest(unittest.TestCase):
    def test_sweep_not_required_when_the_skill_has_no_corpus(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "jj-agent-review"
            (skill_dir / "references").mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("x")
            (skill_dir / "references" / "maintainer-standards.md").write_text("y")
            transcript = (f"/bin/bash -lc cat {skill_dir}/SKILL.md\n"
                          f"/bin/bash -lc cat {skill_dir}/references/maintainer-standards.md\n")
            self.assertEqual(skill.missing_skill_steps(transcript, skill_dir), [])
            self.assertEqual(skill.missing_skill_steps(f"/bin/bash -lc cat {skill_dir}/SKILL.md\n", skill_dir),
                             ["read references/maintainer-standards.md"])


class PromptsJJAgentTest(unittest.TestCase):
    def test_prompts_mention_jj_agent_rules_and_keep_placeholders(self):
        prompts = ROOT / "prompts"
        review = (prompts / "review.md").read_text(encoding="utf-8")
        for key in ("{repo}", "{number}", "{head}", "{login}", "{worktree}", "{ctx_dir}", "{skill_dir}",
                    "{skill_commit}", "{tier}", "{tier_hint}"):
            self.assertIn(key, review)
        self.assertIn("server binding", review)  # red lines are listed
        self.assertIn("capcut-investigation", review)
        self.assertIn("40 ms", review)
        self.assertNotIn("sglang", review.lower())
        for name in ("review_delta.md", "verify.md", "ci_triage.md"):
            text = (prompts / name).read_text(encoding="utf-8")
            self.assertNotIn("sglang", text.lower(), name)
        self.assertIn("{findings_json}", (prompts / "verify.md").read_text(encoding="utf-8"))
        self.assertIn("{related_tests}", (prompts / "ci_triage.md").read_text(encoding="utf-8"))
        self.assertIn("ubuntu-24.04", (prompts / "ci_triage.md").read_text(encoding="utf-8"))

    def test_repo_skill_has_frontmatter_and_standards(self):
        skill_md = ROOT.parent / ".claude" / "skills" / "jj-agent-review" / "SKILL.md"
        if not skill_md.exists():  # the adapter tests also run from a standalone checkout
            self.skipTest("jj-agent repository skill not alongside this checkout")
        text = skill_md.read_text(encoding="utf-8")
        self.assertTrue(re.match(r"^---\nname: jj-agent-review\ndescription: ", text))
        self.assertTrue((skill_md.parent / "references" / "maintainer-standards.md").exists())


if __name__ == "__main__":
    unittest.main()
