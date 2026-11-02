"""Cheap, deterministic triage for jj-agent PRs, run before any agent time is
spent: risk areas from paths, AI-authorship signals, readiness gates and a
tier. This file replaces the SGLang version of sglang-review-inbox
(inbox/triage.py); the function signatures are unchanged so inbox/pipeline.py
and server.py work as before."""

import re

# Ordered most specific first; the first matching prefix wins.
# "high" = the maintainers' red-line surfaces (local server, path handling,
# ffmpeg argv, overwrite protection, network, packaging); see
# .claude/skills/jj-agent-review/references/maintainer-standards.md §1.
RISK_PATHS = [
    ("high", "clean-room material", "reference/capcut-investigation/"),
    ("high", "clip engine / export", "scripts/clip.py"),
    ("high", "project / export", "scripts/project.py"),
    ("high", "local server", "scripts/app_server.py"),
    ("high", "launcher", "scripts/run_app.py"),
    ("high", "packaging", "scripts/build_package.py"),
    ("high", "packaging", "scripts/package_check.py"),
    ("high", "network (AI naming)", "scripts/ai_editor.py"),
    ("high", "network (recorder)", "scripts/recorder.py"),
    ("high", "page logic (save / send / keys)", "app/app.js"),
    ("medium", "speech / cleanup", "scripts/asr_engine.py"),
    ("medium", "speech / cleanup", "scripts/cleanup.py"),
    ("medium", "streamer-facing text", "scripts/friendly.py"),
    ("medium", "page", "app/"),
    ("medium", "scripts other", "scripts/"),
    ("medium", "licences", "LICENSES.md"),
    ("medium", "licences", "licenses/"),
    ("medium", "licences", "第三方组件许可.txt"),
    ("medium", "CI", ".github/"),
    ("medium", "launcher", "切片助手.bat"),
    ("medium", "launcher", "启动切片助手.vbs"),
    ("low", "tests", "tests/"),
    ("low", "docs", "reference/"),
    ("low", "experiments", "experiments/"),
]
RANK = {"low": 0, "medium": 1, "high": 2}

# Files whose change means the export path moved: the PR must carry the
# synthetic flash + beep A/V sync readings (<= 40 ms per seam).
EXPORT_PATHS = ("scripts/clip.py", "scripts/project.py")
SYNC_EVIDENCE = re.compile(r"音画|同步|a/v|av sync|sync|\b\d+(\.\d+)?\s*ms\b|毫秒", re.I)
CLEANROOM = re.compile(r"capcut-investigation", re.I)

AI_MARKERS = [
    (re.compile(r"generated with \[?claude code", re.I), "Claude Code"),
    (re.compile(r"co-authored-by:\s*claude|noreply@anthropic\.com", re.I), "Claude co-author"),
    (re.compile(r"\bcodex\b|chatgpt|openai\.com", re.I), "Codex/ChatGPT"),
    (re.compile(r"\bcursor(agent)?\b|copilot", re.I), "Cursor/Copilot"),
    (re.compile(r"🤖"), "🤖 marker"),
]
TEST_EVIDENCE = re.compile(
    r"unittest|Ran \d+ tests?|OK \(|OK\b|ruff|test_\w+\.py|python3? -[BI]|没跑|passed|```", re.I)
PERF_TITLE = re.compile(r"\[perf\]|speed ?up|optimi[sz]|faster|latency|throughput|提速|更快", re.I)
PERF_NUMBERS = re.compile(r"\d[\d.,]*\s*(ms|s\b|秒|x\b|×|%|倍)", re.I)


def classify_path(path):
    for level, area, prefix in RISK_PATHS:
        if path.startswith(prefix):
            return level, area
    if path.endswith((".md", ".mdx", ".rst", ".txt")):
        return "low", "docs"
    return "medium", "other"


def ai_signals(pr):
    texts = [pr.get("body") or ""]
    for commit in pr.get("commits") or []:
        texts.append(f"{commit.get('messageHeadline', '')}\n{commit.get('messageBody', '')}")
        for author in commit.get("authors") or []:
            texts.append(author.get("email") or "")
    blob = "\n".join(texts)
    return sorted({label for pattern, label in AI_MARKERS if pattern.search(blob)})


def triage(pr, policy, requested_by_human=False):
    files = pr.get("files") or []
    lines = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    areas, level = {}, "low"
    paths = [item.get("path", "") for item in files]
    for item in files:
        file_level, area = classify_path(item.get("path", ""))
        churn = (item.get("additions") or 0) + (item.get("deletions") or 0)
        areas[area] = areas.get(area, 0) + churn
        if RANK[file_level] > RANK[level]:
            level = file_level
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    touches_tests = any(path.startswith("tests/") for path in paths)
    touches_export = any(path in EXPORT_PATHS for path in paths)

    # (id, kind, reason shown on the dashboard, what to ask the author; "" = don't ask)
    gates = []
    if pr.get("state") != "OPEN":
        gates.append(("closed", "closed", f"PR is {pr.get('state', '').lower()}", ""))
    if pr.get("isDraft"):
        gates.append(("draft", "author", "still a draft", ""))  # the author's own choice; never nag
    if pr.get("mergeable") == "CONFLICTING":
        gates.append(("conflict", "merge", "merge conflict with main",
                      "rebase onto main and resolve the merge conflicts"))
    if lines > policy["max_changed_lines"]:
        gates.append(("size", "author", f"{lines} changed lines, over the {policy['max_changed_lines']} limit",
                      f"split it below {policy['max_changed_lines']} changed lines (now {lines})"))
    if any(path.startswith("reference/capcut-investigation/") for path in paths) or CLEANROOM.search(body):
        # Clean-room rule (ADR-003): never a question for the author, always for a maintainer.
        gates.append(("cleanroom", "human", "touches or cites reference/capcut-investigation/ (clean-room rule)", ""))
    if policy["require_test_plan"] and level != "low" and not touches_tests and not TEST_EVIDENCE.search(body):
        gates.append(("tests", "author", "no test commands or results in the description",
                      "add the unittest / ruff commands and their numbers for the current head to the description"))
    if touches_export and not SYNC_EVIDENCE.search(body):
        gates.append(("avsync", "author", "export code changed but no A/V sync readings in the description",
                      "run the synthetic flash + beep sync check and add the per-seam readings (<= 40 ms) for FLV and MP4"))
    if policy["require_perf_numbers"] and PERF_TITLE.search(title) and not PERF_NUMBERS.search(body):
        gates.append(("perf", "author", "perf PR without before/after numbers",
                      "add before/after numbers with the machine and the recording used"))

    hot = level == "high"
    if hot and (lines > 400 or "local server" in areas or "packaging" in areas or "clean-room material" in areas):
        tier = "T3"
    elif "network (AI naming)" in areas or "network (recorder)" in areas:
        tier = "T3"  # the only places network is allowed: always a maintainer's eyes
    elif level == "low" or (lines < 80 and not hot):
        tier = "T1"
    else:
        tier = "T2"

    return {
        "lines": lines,
        "files": len(files),
        "risk_level": level,
        "areas": dict(sorted(areas.items(), key=lambda kv: -kv[1])),
        "ai_signals": ai_signals(pr),
        "gates": [{"id": gate_id, "kind": kind, "reason": reason, "ask": ask, "tell_author": bool(ask)}
                  for gate_id, kind, reason, ask in gates],
        "tier": tier,
        "requested_by_human": requested_by_human,
    }


def priority(card, triage_result):
    """Agent-queue order: explicit human asks first, then smaller PRs."""
    score = 0.0
    requesters = card.get("requesters") or []
    if any(r.get("source") == "slack" for r in requesters):
        score += 50
    score += 10 * len({r.get("author") for r in requesters})
    score -= min(triage_result["lines"], 3000) / 100
    return score
