"""Cheap, deterministic triage that runs before any agent time is spent:
risk areas from paths, AI-authorship signals, readiness gates and a tier."""

import re

# Ordered most specific first; the first matching prefix wins.
RISK_PATHS = [
    ("high", "scheduler/manager", "python/sglang/srt/managers/"),
    ("high", "KV/mem cache", "python/sglang/srt/mem_cache/"),
    ("high", "model executor / CUDA graph", "python/sglang/srt/model_executor/"),
    ("high", "attention backend", "python/sglang/srt/layers/attention/"),
    ("high", "MoE", "python/sglang/srt/layers/moe/"),
    ("high", "quantization", "python/sglang/srt/layers/quantization/"),
    ("high", "distributed", "python/sglang/srt/distributed/"),
    ("high", "speculative", "python/sglang/srt/speculative/"),
    ("high", "server args / defaults", "python/sglang/srt/server_args.py"),
    ("high", "server args / defaults", "python/sglang/srt/arg_groups/"),
    ("high", "API entrypoints", "python/sglang/srt/entrypoints/"),
    ("high", "PD disaggregation", "python/sglang/srt/disaggregation/"),
    ("high", "sampling", "python/sglang/srt/sampling/"),
    ("high", "kernels", "python/sglang/kernels/"),
    ("high", "kernels", "python/sglang/jit_kernel/"),
    ("high", "kernels", "sgl-kernel/"),
    ("medium", "model implementation", "python/sglang/srt/models/"),
    ("medium", "diffusion", "python/sglang/multimodal_gen/"),
    ("medium", "layers", "python/sglang/srt/layers/"),
    ("medium", "runtime other", "python/sglang/srt/"),
    ("low", "tests", "test/"),
    ("low", "docs", "docs/"),
    ("low", "benchmark", "benchmark/"),
    ("low", "CI", ".github/"),
    ("low", "scripts", "scripts/"),
    ("low", "examples", "examples/"),
]
RANK = {"low": 0, "medium": 1, "high": 2}

AI_MARKERS = [
    (re.compile(r"generated with \[?claude code", re.I), "Claude Code"),
    (re.compile(r"co-authored-by:\s*claude|noreply@anthropic\.com", re.I), "Claude co-author"),
    (re.compile(r"\bcodex\b|chatgpt|openai\.com", re.I), "Codex/ChatGPT"),
    (re.compile(r"\bcursor(agent)?\b|copilot", re.I), "Cursor/Copilot"),
    (re.compile(r"🤖"), "🤖 marker"),
]
TEST_EVIDENCE = re.compile(
    r"pytest|unittest|python3? -m|test_\w+\.py|gsm8k|mmlu|accuracy|bench_\w+|benchmark|"
    r"/rerun-test|passed|tok/s|throughput|```", re.I)
PERF_TITLE = re.compile(r"\[perf\]|speed ?up|optimi[sz]|faster|latency|throughput", re.I)
PERF_NUMBERS = re.compile(r"\d[\d.,]*\s*(ms|us|µs|s\b|tok/s|tokens/s|x\b|×|%|qps|req/s|gb/s|tflops)", re.I)


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
    for item in files:
        file_level, area = classify_path(item.get("path", ""))
        churn = (item.get("additions") or 0) + (item.get("deletions") or 0)
        areas[area] = areas.get(area, 0) + churn
        if RANK[file_level] > RANK[level]:
            level = file_level
    body = pr.get("body") or ""
    title = pr.get("title") or ""
    touches_tests = any(item.get("path", "").startswith("test/") for item in files)

    # (id, kind, reason shown on the dashboard, what to ask the author; "" = don't ask)
    gates = []
    if pr.get("state") != "OPEN":
        gates.append(("closed", "closed", f"PR is {pr.get('state', '').lower()}", ""))
    if pr.get("isDraft"):
        gates.append(("draft", "author", "still a draft", ""))  # the author's own choice; never nag
    if pr.get("mergeable") == "CONFLICTING":
        # A merge concern, not a review one: the code is still reviewed and approved.
        gates.append(("conflict", "merge", "merge conflict with main",
                      "rebase onto main and resolve the merge conflicts"))
    if lines > policy["max_changed_lines"]:
        gates.append(("size", "author", f"{lines} changed lines, over the {policy['max_changed_lines']} limit",
                      f"split it below {policy['max_changed_lines']} changed lines (now {lines})"))
    if policy["require_test_plan"] and level != "low" and not touches_tests and not TEST_EVIDENCE.search(body):
        gates.append(("tests", "author", "no test commands or results in the description",
                      "add the test commands and results for the current head to the description"))
    if policy["require_perf_numbers"] and PERF_TITLE.search(title) and not PERF_NUMBERS.search(body):
        gates.append(("perf", "author", "perf PR without before/after numbers",
                      "add before/after numbers with hardware and workload"))

    hot = level == "high"
    model_lines = areas.get("model implementation", 0)
    if hot and (lines > 400 or "server args / defaults" in areas or "API entrypoints" in areas):
        tier = "T3"
    elif model_lines > 1500 or areas.get("kernels", 0) > 300:
        tier = "T3"
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
