"""CI policy for jj-agent PRs. Replaces the SGLang version of
sglang-review-inbox (inbox/ci.py); same public names so pipeline.py works.

jj-agent's CI (.github/workflows/tests.yml) is one workflow, `tests`, with a
two-leg matrix:

- `tests / linux (ubuntu-24.04)`  FFmpeg 6.1: the REQUIRED gate (ADR-003 D2);
- `tests / linux (ubuntu-latest)` FFmpeg 8: continue-on-error, reported only.

There are no run-ci labels, no gate jobs and no slash commands (/rerun-test,
/tag-and-rerun-ci do not exist here), so this module never suggests a CI
command: `suggestions` is always empty and reruns are a maintainer's
`gh run rerun --failed`. Evidence must still come from the current head;
skipped / cancelled / zero-test jobs are not passes."""

import hashlib
import re

from . import github
from .github import summarize_checks

# Kept for pipeline.py, which checks `RUN_CI_LABEL not in labels` before it
# would send /tag-and-rerun-ci. jj-agent has no such label; with the policy
# switches off in config.json the command is never sent, and the label name
# below never matches a real label.
RUN_CI_LABEL = "run-ci"
BASE_GATE = re.compile(r"^tests / linux \(ubuntu-24\.04\)$")
TARGETED_PREFIX = "targeted / "

REQUIRED_JOB = re.compile(r"linux \(ubuntu-24\.04\)")
OPTIONAL_JOB = re.compile(r"linux \(ubuntu-latest\)")

# No hardware lanes: every check is "base". The two functions stay so that
# pipeline.py's calls keep working.
PLATFORM_LANES = {}
PLATFORM_HINTS = {}


def signature(failures):
    names = sorted(f["name"] for f in failures)
    return hashlib.sha1("\n".join(names).encode()).hexdigest()[:12]


def lane_platform(check_name):
    return None


def pr_platforms(snapshot):
    return set()


def is_required(check_name):
    """ubuntu-24.04 is the gate; ubuntu-latest is continue-on-error (noted,
    never blocking) until T16 closes it. Anything else (a future job) is
    treated as required so a new gate is never silently ignored."""
    if OPTIONAL_JOB.search(check_name):
        return False
    return True


# Rules that classify a CI failure without the agent: only CI-infrastructure
# signals from GitHub-hosted runners and the apt/pip/npm install steps.
SETUP_STEP = re.compile(r"^(Set up job|Initialize containers|Run actions/checkout|Run actions/setup-"
                        r"|System tools|Python and browser-test tools|Post )", re.I)
INFRA_LOG = re.compile(r"No space left on device|lost communication with the server"
                       r"|runner has received a shutdown signal|hosted runner encountered an error"
                       r"|Could not resolve host|API rate limit exceeded|Unable to download artifact"
                       r"|E: Unable to (locate|fetch)|Temporary failure resolving|Hash Sum mismatch"
                       r"|npm ERR! (network|ETIMEDOUT|ECONNRESET)|ReadTimeoutError|Connection timed out"
                       r"|(429|50[234]) (Client|Server) Error", re.I)
STATUS_JOB = re.compile(r"^(wait-for-[\w-]+|[\w-]+-finish)$")


def rule_triage(failures):
    """Classify what the rules can; each failure needs `log_tail` and may have
    `failed_step`. A failure without a readable log is left `unclassified`."""
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
    """Return the CI assessment for the PR's current head. Pure function.

    ci_triage: the latest agent classification for this head and signature.
    extra_failures / exhausted: kept for pipeline.py; jj-agent sends no CI
    commands, so `exhausted` only decides whether a cancelled job is reported
    as such. waived: job names the author showed to be unrelated."""
    head = snapshot["headRefOid"]
    checks = summarize_checks(snapshot.get("statusCheckRollup"))
    counts = checks["counts"]
    held = checks["gated"]  # jj-agent has no gate job; normally empty
    counts["not_requested"] = 0

    failures = [f for f in checks["failures"] if is_required(f["name"])] + list(extra_failures)
    pending = [n for n in checks["pending"] if is_required(n)]
    cancelled = [n for n in checks["cancelled"] if is_required(n)]
    optional_failures = [f for f in checks["failures"] if not is_required(f["name"])]
    notes = []
    if optional_failures:
        notes.append(f"{len(optional_failures)} failure(s) on the report-only ubuntu-latest leg (FFmpeg 8, "
                     "continue-on-error until T16): " + ", ".join(sorted(f["name"] for f in optional_failures)))

    suggestions = []  # no CI slash commands exist in jj-agent

    if pending:
        status = "running"
    elif failures:
        status = "failed"
    elif held:
        status = "gated"
    elif cancelled:
        status = "cancelled"
    elif counts["success"] == 0:
        status = "not_started"
    else:
        status = "green"
    if cancelled:
        notes.append(f"{len(cancelled)} required job(s) cancelled (a newer push or a manual cancel); "
                     "cancelled is not passed: " + ", ".join(cancelled[:3]))
    if status == "not_started":
        notes.append("no check has run on this head yet; the `tests` workflow runs on every push")

    blocking, unrelated, retryable = [], [], []
    sig = signature(failures)
    if status == "failed":
        classified, no_log, pending_agent = {}, set(), set()
        if ci_triage and ci_triage.get("signature") == sig:
            classified = {f["name"]: f for f in ci_triage.get("failures", [])}
            no_log = set(ci_triage.get("no_log") or [])
            pending_agent = set(ci_triage.get("pending") or [])
        for failure in failures:
            verdict = classified.get(failure["name"], {}).get("classification", "unclassified")
            evidence = classified.get(failure["name"], {}).get("evidence", "")
            if failure["name"] in no_log:
                verdict = "unclassified" if verdict == "related" else verdict
                evidence = ""
            failure = dict(failure, classification=verdict, evidence=evidence,
                           pending_agent=failure["name"] in pending_agent)
            if any(name and name in failure["name"] for name in waived):
                unrelated.append(dict(failure, classification="unrelated, the author explained it"))
                continue
            if verdict in ("infra", "flaky"):
                # Nothing to send automatically: a maintainer reruns with `gh run rerun --failed`.
                retryable.append(dict(failure, family="rerun-failed"))
                notes.append(f"{failure['name']} looks {verdict}; rerun by hand: gh run rerun <run-id> --failed")
            elif verdict == "unrelated":
                unrelated.append(failure)
            else:
                blocking.append(failure)

    needs_triage = status == "failed" and not (ci_triage and ci_triage.get("signature") == sig)
    not_run = bool(pending or held or (status == "cancelled"))
    passed = not not_run and status in ("green", "failed") and not blocking and not retryable
    return {
        "head": head,
        "status": status,
        "checks": checks,
        "platforms": [],
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
