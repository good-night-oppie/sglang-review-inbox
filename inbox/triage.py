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
    ("low", "tests", "test/"),  # MontClip / MontClip-Cindy keep their tests under test/
    ("low", "docs", "reference/"),
    ("low", "experiments", "experiments/"),
]
RANK = {"low": 0, "medium": 1, "high": 2}

# Files whose change means the export path moved: the PR must carry the
# synthetic flash + beep A/V sync readings (<= 40 ms per seam).
EXPORT_PATHS = ("scripts/clip.py", "scripts/project.py")
# --- Size gate: what counts as "lines to review" -------------------------------------------------------
# Only two kinds of lines are ever left out of the size gate's count; the total additions+deletions is always kept
# and shown ("lines" in the result):
#   1. lockfiles, matched by exact file name only (never a glob, never a directory). This only affects the size
#      reminder; it adds no blocking of its own;
#   2. files in a QQ size exception: a record of QQ's accepted review, bound to the exact current head and to an
#      exact file list, plus the evidence that fits the file's type (third-party copy vs. this repo's generated output).
# Everything doubtful is counted in full: missing / unreadable / stale evidence, a review that is not "accepted", a
# truncated file list, a file that deviates from its upstream, a path the review does not list.
# Filled-in fields never turn into an approved exception by themselves: the record must carry QQ's review. The
# licence, byte-for-byte and reproduction conclusions are QQ's recorded judgments; this code only checks that they are
# recorded and bound to this head, it does not verify them. The PR description is never evidence.
# ORIGIN IS NOT AUTHENTICATED HERE. "by": "QQ" and "verdict": "accepted" are record content, not proof of who wrote the
# record. Records are therefore applied only when policy["size_exceptions_trusted_source"] is True, which only a
# controlled review-record entry may set; absent / False / anything else = the records are ignored (fail closed).
# Exceptions are read from policy and from nowhere else: never from the PR description, from files in the PR, or from
# any agent / task input. Who may write that entry, how its origin is checked and how changes are logged is the storage
# design (open); this code only refuses to act without that attestation.
# Where the records are stored is NOT decided here: whatever store is approved must supply policy["size_exceptions"].
LOCKFILES = re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock)$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PR_URL = re.compile(r"github\.com/([^/]+/[^/]+)/pull/(\d+)")


def pr_key(pr):
    match = PR_URL.search(pr.get("url") or "")
    return f"{match.group(1)}#{match.group(2)}" if match else ""


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _exact_paths(value):
    return (isinstance(value, list) and bool(value)
            and all(_text(p) and not p.endswith("/") and not any(c in p for c in "*?[") for p in value))


def _shown(a, b):
    """DISPLAY ONLY: the shortest prefix (at least 8 chars) that tells two heads apart. Never used for matching."""
    n = 8
    while n < 40 and a[:n] == b[:n]:
        n += 1
    return a[:n], b[:n]


def _review_problem(entry, head):
    """Why the record cannot be trusted as QQ's accepted review of THIS head (None = it can)."""
    review = entry.get("review") if isinstance(entry, dict) else None
    if not isinstance(review, dict):
        return "no QQ review recorded" if isinstance(entry, dict) else "record unreadable"
    if _text(review.get("by")).upper() != "QQ":
        return "review is not recorded as QQ's"
    if not _text(review.get("source")):
        return "review source not recorded"
    verdict = _text(review.get("verdict")).lower()
    if verdict != "accepted":
        return f"review verdict is '{verdict or 'missing'}', not accepted"
    reviewed = _text(review.get("head")).lower()
    if not HEX40.match(reviewed):
        return "review head is not an exact 40-hex sha"
    if not HEX40.match(head):
        return "PR head unknown"
    if reviewed != head:  # full 40-hex equality is the only authorization test; a prefix is never enough
        shown_reviewed, shown_head = _shown(reviewed, head)
        return f"stale: reviewed {shown_reviewed} but the PR head is {shown_head}"
    if not _exact_paths(review.get("files")):
        return "review does not list exact files"
    return None


def _group_problem(group):
    """Why one evidence group cannot be applied (None = the evidence is recorded and well formed).
    The type decides what is needed: a third-party copy needs its upstream; this repo's generated output must not be
    given an invented upstream and needs its inputs, command, result hashes and reproduction record instead."""
    if not isinstance(group, dict):
        return "group unreadable"
    if not _exact_paths(group.get("files")):
        return "no exact file list (no globs, no directories)"
    kind = group.get("kind")
    if kind == "vendored":
        upstream, verified = group.get("upstream"), group.get("verified")
        upstream, verified = upstream if isinstance(upstream, dict) else {}, verified if isinstance(verified, dict) else {}
        if not _text(upstream.get("repo")):
            return "no upstream repo"
        if not HEX40.match(_text(upstream.get("commit"))):
            return "upstream commit is not an immutable 40-hex sha"
        if not _text(group.get("licence")):
            return "no licence recorded"
        if verified.get("byte_identical") is not True or not _text(verified.get("method")):
            return "no byte-for-byte check recorded"
        return None
    if kind == "generated":
        generated = group.get("generated") if isinstance(group.get("generated"), dict) else {}
        if not _exact_paths(generated.get("inputs")):
            return "no exact input files recorded"
        if not _text(generated.get("command")):
            return "no generation command recorded"
        hashes = generated.get("result_sha256")
        if not isinstance(hashes, dict) or not all(HEX64.match(_text(hashes.get(p)).lower()) for p in group["files"]):
            return "result hash missing for a listed file"
        if generated.get("reproduced") is not True or not _text(generated.get("record")):
            return "reproduction not recorded"
        return None
    return "unknown kind"


def size_exception(pr, policy, paths):
    """QQ's size exception for this PR against its complete file list: ({path: why}, status or None)."""
    exceptions = policy.get("size_exceptions")
    entry = exceptions.get(pr_key(pr)) if isinstance(exceptions, dict) else None
    if not entry:
        return {}, None
    if policy.get("size_exceptions_trusted_source") is not True:
        return {}, "not applied: record origin not confirmed (no controlled review-record entry is attested)"
    try:
        problem = _review_problem(entry, _text(pr.get("headRefOid")).lower())
        if problem:
            return {}, "not applied: " + problem
        groups = entry.get("groups")
        if not isinstance(groups, list) or not groups:
            return {}, "not applied: no evidence groups"
        reviewed, exempt, notes = set(entry["review"]["files"]), {}, []
        for group in groups:
            problem = _group_problem(group)
            if problem:
                notes.append(f"{group.get('kind') if isinstance(group, dict) else '?'} group not applied ({problem})")
                continue
            # A file that deviates from its upstream is counted in full; so is any path the review does not list.
            deviating = set((group.get("verified") or {}).get("deviating") or []) if group["kind"] == "vendored" else set()
            for path in group["files"]:
                if path in paths and path in reviewed and path not in deviating:
                    exempt[path] = f"QQ-recorded exception, {group['kind']}"
        if exempt:
            return exempt, "applied to " + str(len(exempt)) + " file(s)" + ("; " + "; ".join(notes) if notes else "")
        return {}, "not applied: " + ("; ".join(notes) if notes else "no reviewed file is in this PR")
    except (AttributeError, TypeError, KeyError):
        return {}, "not applied: record unreadable"


def reviewable_lines(pr, policy):
    """Split the changed lines into what counts against the size gate and what does not (see the block above).
    If the file list is incomplete (fewer files than changedFiles, or the query failed) nothing is excluded, not even a
    lockfile. Measured on 184 cards: the current fetch path (github.py fetch_pr: gh pr view <n> --repo <repo> --json
    <PR_FIELDS>, gh 2.73.0, no paging argument) returned at most 100 files; this says nothing about other paths."""
    total = (pr.get("additions") or 0) + (pr.get("deletions") or 0)
    files = pr.get("files") or []
    expected = pr.get("changedFiles") or 0
    out = {"total": total, "counted": total, "excluded": 0, "excluded_files": [], "exception": None, "incomplete": False}
    exceptions = policy.get("size_exceptions")
    recorded = isinstance(exceptions, dict) and exceptions.get(pr_key(pr))
    if not files or len(files) < expected:
        out["incomplete"] = expected > len(files)  # also true when no files came back for a non-empty PR
        if recorded:
            out["exception"] = "not applied: file list incomplete, all lines counted"
        return out
    exempt, out["exception"] = size_exception(pr, policy, {item.get("path", "") for item in files})
    for item in files:
        name = item.get("path", "")
        why = "lockfile" if LOCKFILES.search(name) else exempt.get(name)
        if why:
            churn = (item.get("additions") or 0) + (item.get("deletions") or 0)
            out["excluded_files"].append({"path": name, "lines": churn, "why": why})
            out["excluded"] += churn
    out["counted"] = total - out["excluded"]
    return out


def provenance_gate(sizes):
    """A maintainer must look at every change the size gate does not count, and at any size exception that was
    recorded but not applied. Never satisfied by the PR body. kind=human: it blocks nothing, it makes the check visible."""
    parts, asks = [], []
    if sizes["incomplete"]:
        parts.append("the file list is incomplete, so lockfile / vendored changes cannot be ruled out")
    shown = sizes["excluded_files"]
    if shown:
        names = "; ".join(f"{f['path']} ({f['lines']} lines, {f['why']})" for f in shown[:8])
        names += f"; +{len(shown) - 8} more" if len(shown) > 8 else ""
        parts.append(f"{sizes['excluded']} of {sizes['total']} changed lines are not counted toward the size gate: {names}")
        if any(f["why"] == "lockfile" for f in shown):
            asks.append("lockfile: packages added / removed / upgraded, the hosts of the resolved URLs, install scripts")
        if any(f["why"] != "lockfile" for f in shown):
            asks.append("The exception covers the size gate only; it is not a code approval or a merge permission. "
                        "Licence, byte-for-byte and reproduction conclusions are QQ's recorded judgments, not verified by this tool")
    if sizes["exception"] and "not applied" in sizes["exception"]:
        # three different outcomes, three different wordings (none of them says that every line is counted):
        #   partly applied  -> the status names the group that failed and why; its files are counted, the rest stays
        #   nothing applied -> normal counting is restored (only the defined lockfile exclusion stays)
        #   list incomplete -> no file can be excluded at all
        if sizes["exception"].startswith("applied"):
            tail = "the files of the group that is not applied are counted in full; the groups recorded completely stay applied"
        elif sizes["incomplete"]:
            tail = "no file is excluded, a lockfile cannot be reliably told apart without the full file list"
        else:
            tail = "normal counting is restored, only the defined lockfile exclusion stays"
        parts.append(f"QQ size exception {sizes['exception']}; {tail}")
    if not parts:
        return None
    reason = "provenance check needed: " + "; ".join(parts)
    if asks:
        reason += ". " + "; ".join(asks)
    return ("provenance", "human", reason, "")


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
    r"unittest|Ran \d+ tests?|OK \(|OK\b|ruff|test_\w+\.py|python3? -[BI]|没跑|passed|```"
    # MontClip / MontClip-Cindy (Node): 「共 N，通过 N，失败 N」 and the npm / node --test commands
    r"|共\s*\d+\s*[，,]\s*通过\s*\d+\s*[，,]\s*失败\s*\d+|npm (run )?test|node --test", re.I)
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
    touches_tests = any(path.startswith(("tests/", "test/")) for path in paths)
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
    sizes = reviewable_lines(pr, policy)
    counted, excluded = sizes["counted"], sizes["excluded"]
    if counted > policy["max_changed_lines"]:
        note = f" ({sizes['total']} total additions+deletions, {excluded} not counted: lockfile / QQ size exception)" if excluded else ""
        gates.append(("size", "author", f"{counted} changed lines, over the {policy['max_changed_lines']} limit{note}",
                      f"split it below {policy['max_changed_lines']} changed lines (now {counted})"))
    provenance = provenance_gate(sizes)
    if provenance:
        gates.append(provenance)
    if any(path.startswith("reference/capcut-investigation/") for path in paths) or CLEANROOM.search(body):
        # Clean-room rule (ADR-003): never a question for the author, always for a maintainer.
        gates.append(("cleanroom", "human", "touches or cites reference/capcut-investigation/ (clean-room rule)", ""))
    if policy["require_test_plan"] and level != "low" and not touches_tests and not TEST_EVIDENCE.search(body):
        gates.append(("tests", "author", "no test commands or results in the description",
                      "add the test commands (unittest / ruff, or npm test / node --test) and their numbers for the current head to the description"))
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
        "counted_lines": counted,
        "excluded_lines": excluded,
        "excluded_files": sizes["excluded_files"],
        "size_exception": sizes["exception"],
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
