"""CI policy for SGLang PRs, encoding the maintainer's merge bar:

- evidence must come from the current head; old-head passes do not count;
- skipped / cancelled / zero-test jobs are not passes;
- only lanes that matter for the change are required: an NVIDIA-only change
  does not wait on AMD / NPU / XPU / ... lanes, and their failures are noted,
  not blocking;
- infra / flaky failures are retried a bounded number of times; what still
  fails afterwards and is unrelated to the diff is noted, not blocking;
- mergeable = related tests passed on this head, every other failure shown
  to be unrelated, no conflict, and an approval.

Pure functions: they return a CI assessment and the commands that would
advance it. The pipeline decides when to execute them and how often."""

import hashlib
import re

from . import github
from .github import summarize_checks

RUN_CI_LABEL = "run-ci"
# The gate of the base NVIDIA lane. Other lanes (Extra, Extra AMD, MLU, ...) need
# their own opt-in label, so their gate failing only means "not requested".
BASE_GATE = re.compile(r"^PR Test Base /")
TARGETED_PREFIX = "targeted / "

# Workflow-name marker of a hardware lane -> platform, and what in a PR's title or
# changed paths makes that platform relevant to it.
PLATFORM_LANES = {
    "(AMD)": "amd", "(NPU)": "npu", "(XPU)": "xpu", "(MUSA)": "musa", "(MLU)": "mlu",
    "(Arm64)": "cpu", "(Xeon)": "cpu", "(MLX)": "mlx", "(PPU)": "ppu", "(sgl-router)": "router",
}
PLATFORM_HINTS = {
    "amd": re.compile(r"\[amd\]|rocm|\bhip\b|_hip|hip_|aiter|gfx9\d+|mi3\d\d|\bamd\b|_amd|amd_", re.I),
    "npu": re.compile(r"\[npu\]|npu|ascend", re.I),
    "xpu": re.compile(r"\[xpu\]|xpu", re.I),
    "musa": re.compile(r"musa", re.I),
    "mlu": re.compile(r"\bmlu|cambricon", re.I),
    "cpu": re.compile(r"\[cpu\]|/cpu/|_cpu\.py|arm64|aarch64|xeon|\bamx\b", re.I),
    "mlx": re.compile(r"\bmlx|/metal/", re.I),
    "ppu": re.compile(r"\bppu", re.I),
    "router": re.compile(r"sgl-router|sgl_router|/router/", re.I),
}


def signature(failures):
    names = sorted(f["name"] for f in failures)
    return hashlib.sha1("\n".join(names).encode()).hexdigest()[:12]


def lane_platform(check_name):
    workflow = check_name.split(" / ")[0]
    return next((platform for marker, platform in PLATFORM_LANES.items() if marker in workflow), None)


def pr_platforms(snapshot):
    text = " ".join([snapshot.get("title") or ""] + [f.get("path", "") for f in snapshot.get("files") or []])
    return {platform for platform, hint in PLATFORM_HINTS.items() if hint.search(text)}


# Rules that classify a CI failure without the agent. Only CI-infrastructure
# signals: a crashed server also shows "connection reset", so generic network
# errors are left to the agent.
SETUP_STEP = re.compile(r"^(Set up job|Initialize containers|Run actions/checkout|Resolve runner_config|Export "
                        r"|Download artifacts|Run \./\.github/actions/(check-pr-test-health|check-maintenance"
                        r"|wait-for-jobs|download-[\w-]+))", re.I)
INFRA_LOG = re.compile(r"No space left on device|lost communication with the server"
                       r"|runner has received a shutdown signal|hosted runner encountered an error"
                       r"|Could not resolve host: github\.com|API rate limit exceeded|Unable to download artifact"
                       r"|Error response from daemon|failed to pull image"
                       r"|(429|50[234]) (Client|Server) Error[^\n]*huggingface\.co", re.I)
STATUS_JOB = re.compile(r"^(wait-for-[\w-]+|pr-test-finish|[\w-]+-finish)$")


def rule_triage(failures):
    """Classify what the rules can; each failure needs `log_tail` and may have
    `failed_step`. A failure without a readable log is left `unclassified`
    (bounded reruns, never the agent: there is nothing to read)."""
    out = []
    for failure in failures:
        job, log = failure["name"].split(" / ")[-1], failure.get("log_tail") or ""
        step = failure.get("failed_step")
        if STATUS_JOB.match(job):
            verdict, evidence = "unrelated", "status job that only reflects the other jobs"
        elif step and SETUP_STEP.match(step):
            verdict, evidence = "infra", f"failed in the setup step '{step}' before any test ran"
        elif github.log_missing(log):
            verdict, evidence = "unclassified", ""
        elif match := INFRA_LOG.search(log):
            line = next((l for l in log.splitlines() if match.group(0) in l), match.group(0))
            verdict, evidence = "infra", line.strip()[-200:]
        else:
            continue
        out.append({"name": failure["name"], "classification": verdict, "evidence": evidence, "by": "rules"})
    return out


def assess(card, snapshot, ci_triage=None, extra_failures=(), exhausted=(), waived=()):
    """ci_triage: the latest agent classification for this head and signature.
    extra_failures: failed jobs of the targeted /rerun-test runs (always required).
    exhausted: retry families ("rerun-failed", "rerun-cancelled", "rerun-test")
    whose attempts are used up; what they could not fix becomes a note.
    waived: job names the author showed to be unrelated (accepted by the agent)."""
    head = snapshot["headRefOid"]
    labels = {label["name"] for label in snapshot.get("labels") or []}
    review = card.get("review") or {}
    checks = summarize_checks(snapshot.get("statusCheckRollup"))
    counts = checks["counts"]
    held = checks["gated"] if RUN_CI_LABEL not in labels else [
        name for name in checks["gated"] if BASE_GATE.search(name)]
    counts["not_requested"] = counts["gated"] - len(held)
    counts["gated"] = len(held)

    platforms = pr_platforms(snapshot)
    required = lambda name: (lane_platform(name) or "base") in platforms | {"base"}  # noqa: E731
    failures = [f for f in checks["failures"] if required(f["name"])] + list(extra_failures)
    pending = [n for n in checks["pending"] if required(n)]
    cancelled = [n for n in checks["cancelled"] if required(n)]
    optional_failures = [f for f in checks["failures"] if not required(f["name"])]
    notes = []
    if optional_failures:
        lanes = sorted({f["name"].split(" / ")[0] for f in optional_failures})
        notes.append(f"{len(optional_failures)} failure(s) on lanes this change does not need: " + ", ".join(lanes))

    suggestions = []
    if RUN_CI_LABEL not in labels:
        suggestions.append({"family": "tag-and-rerun", "command": "/tag-and-rerun-ci",
                            "why": "no run-ci label, so the gate holds CI"})
    targeted = review.get("targeted_tests") or []
    if targeted:
        suggestions.append({"family": "rerun-test", "command": "/rerun-test " + " ".join(targeted),
                            "why": "default CI likely skips the related tests on the target hardware"})

    if pending:
        status = "running"
    elif failures:
        status = "failed"
    elif held:
        status = "gated"  # the run-ci gate (label, cooldown) stopped the tests
    elif cancelled and "rerun-cancelled" not in exhausted:
        status = "cancelled"
    elif counts["success"] == 0:
        status = "not_started"
    else:
        status = "green"
    if cancelled and "rerun-cancelled" in exhausted:
        notes.append(f"{len(cancelled)} required job(s) stayed cancelled after reruns: " + ", ".join(cancelled[:3]))

    if status == "cancelled" and RUN_CI_LABEL in labels:
        suggestions.append({"family": "rerun-cancelled", "command": "/rerun-failed-ci",
                            "why": f"{len(cancelled)} job(s) cancelled before finishing; cancelled is not passed"})
    if status in ("gated", "not_started") and RUN_CI_LABEL in labels:
        suggestions.append({"family": "tag-and-rerun", "command": "/tag-and-rerun-ci",
                            "why": "CI has not started on this head"})

    blocking, unrelated, retryable = [], [], []
    sig = signature(failures)
    if status == "failed":
        classified, no_log, pending = {}, set(), set()
        if ci_triage and ci_triage.get("signature") == sig:
            classified = {f["name"]: f for f in ci_triage.get("failures", [])}
            no_log = set(ci_triage.get("no_log") or [])
            pending = set(ci_triage.get("pending") or [])  # left for the agent's next window
        for failure in failures:
            verdict = classified.get(failure["name"], {}).get("classification", "unclassified")
            evidence = classified.get(failure["name"], {}).get("evidence", "")
            if failure["name"] in no_log:
                # No log was read: a "related" call is a guess, and there is nothing to quote.
                verdict = "unclassified" if verdict == "related" else verdict
                evidence = ""
            failure = dict(failure, classification=verdict, evidence=evidence,
                           pending_agent=failure["name"] in pending)
            if any(name and name in failure["name"] for name in waived):
                unrelated.append(dict(failure, classification="unrelated, the author explained it"))
                continue
            targeted_job = failure["name"].startswith(TARGETED_PREFIX)
            family = "rerun-test" if targeted_job else "rerun-failed"
            if verdict in ("infra", "flaky") and family in exhausted:
                unrelated.append(dict(failure, classification=f"{verdict}, persisted after retries"))
                notes.append(f"{failure['name']} kept failing for {verdict} reasons after retries")
            elif verdict in ("infra", "flaky"):
                retryable.append(dict(failure, family=family))
            elif verdict == "unrelated":
                unrelated.append(failure)
            else:
                blocking.append(failure)
        families = {f["family"] for f in retryable}
        if "rerun-failed" in families:
            suggestions.append({"family": "rerun-failed", "command": "/rerun-failed-ci",
                                "why": "failures look like infra/flaky; bounded retries"})
        if "rerun-test" in families and targeted:
            for suggestion in suggestions:
                if suggestion["family"] == "rerun-test":
                    suggestion.update(retry=True, why="targeted tests failed for infra/flaky reasons; bounded retries")

    needs_triage = status == "failed" and not (ci_triage and ci_triage.get("signature") == sig)
    not_run = bool(pending or held or (status == "cancelled"))
    passed = not not_run and status in ("green", "failed") and not blocking and not retryable
    return {
        "head": head,
        "status": status,
        "checks": checks,
        "platforms": sorted(platforms),
        "failures": failures,
        "signature": sig,
        "blocking": blocking,
        "unrelated": unrelated,
        "retryable": retryable,
        "notes": notes,
        "needs_triage": needs_triage,
        "passed": passed,
        "conflict": snapshot.get("mergeable") == "CONFLICTING",
        "merge_state": snapshot.get("mergeStateStatus"),
        "review_decision": snapshot.get("reviewDecision"),
        "suggestions": suggestions,
    }
