"""Thin wrapper over the authenticated `gh` CLI. Agents never call this; only
the orchestrator does, so every GitHub write goes through one audited place."""

import json
import re
import subprocess

PR_FIELDS = (
    "number,title,author,url,state,isDraft,headRefOid,baseRefName,additions,deletions,"
    "changedFiles,files,labels,mergeable,mergeStateStatus,reviewDecision,body,"
    "statusCheckRollup,commits,latestReviews,createdAt,updatedAt,mergedBy"
)
PR_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
JOB_URL = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)/job/(\d+)")


class GitHubError(RuntimeError):
    pass


TRANSIENT = re.compile(r"EOF|timeout|timed out|connection reset|50[234]|TLS handshake", re.I)


def gh(args, input_text=None, timeout=120, write=False):
    # Retry transient network errors once, but only for reads: a write that
    # failed mid-flight may still have landed, and the ledger handles that.
    for attempt in range(1 if write else 2):
        proc = subprocess.run(["gh", *args], input=input_text, capture_output=True,
                              text=True, timeout=timeout)
        if proc.returncode == 0:
            return proc.stdout
        if not TRANSIENT.search(proc.stderr):
            break
    raise GitHubError(f"gh {' '.join(args[:3])}: {proc.stderr.strip()[:500]}")


def gh_json(args, **kwargs):
    out = gh(args, **kwargs)
    return json.loads(out) if out.strip() else None


def find_pr_refs(text, repos):
    """PR references in free text (Slack formats links as <url|label>)."""
    refs = []
    for repo, number in PR_URL.findall(text or ""):
        if repo in repos and (repo, int(number)) not in refs:
            refs.append((repo, int(number)))
    return refs


def fetch_pr(repo, number):
    return gh_json(["pr", "view", str(number), "--repo", repo, "--json", PR_FIELDS])


GATE_CHECK = re.compile(r"pr-gate|call-gate")
# `pr-test-finish` / `wait-for-stage-b` style jobs only aggregate upstream results.
AGGREGATE_CHECK = re.compile(r"(^|[/ -])finish$|/ wait-for-")


def summarize_checks(rollup):
    """Collapse statusCheckRollup (already scoped to the head commit).

    The rollup keeps superseded attempts, so only the newest result per check
    name counts. Cancelled jobs and the run-ci label gate are reported apart
    from failures: neither is a test result, and neither is a pass."""
    latest = {}
    for item in rollup or []:
        if item.get("__typename") == "StatusContext":
            name = item.get("context", "")
            when = item.get("startedAt") or ""
        else:
            name = f"{item.get('workflowName', '')} / {item.get('name', '')}".strip(" /")
            when = item.get("completedAt") or item.get("startedAt") or ""
        if name not in latest or when >= latest[name][0]:
            latest[name] = (when, item)

    counts = {"success": 0, "failure": 0, "pending": 0, "skipped": 0,
              "cancelled": 0, "gated": 0, "other": 0}
    failures, pending, cancelled, aggregates, gated = [], [], [], [], []
    for name, (_, item) in sorted(latest.items()):
        if item.get("__typename") == "StatusContext":
            url = item.get("targetUrl", "")
            raw = (item.get("state") or "").upper()
            bucket = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure"}.get(raw, "pending")
        else:
            url = item.get("detailsUrl", "")
            raw = item.get("conclusion") or ""
            if item.get("status") != "COMPLETED":
                bucket = "pending"
            else:
                bucket = {
                    "SUCCESS": "success", "NEUTRAL": "success",
                    "FAILURE": "failure", "TIMED_OUT": "failure", "STARTUP_FAILURE": "failure",
                    "CANCELLED": "cancelled", "ACTION_REQUIRED": "pending",
                    "SKIPPED": "skipped", "STALE": "other",
                }.get(raw, "other")
        if bucket == "failure" and GATE_CHECK.search(name):
            bucket = "gated"
        if bucket == "failure" and AGGREGATE_CHECK.search(name):
            aggregates.append({"name": name, "url": url, "conclusion": raw})
            continue
        counts[bucket] += 1
        if bucket == "failure":
            failures.append({"name": name, "url": url, "conclusion": raw,
                             "completed_at": item.get("completedAt") or item.get("startedAt") or ""})
        elif bucket == "pending":
            pending.append(name)
        elif bucket == "cancelled":
            cancelled.append(name)
        elif bucket == "gated":
            gated.append(name)
    # A failed aggregator is a cascade of what it wraps; it is only the
    # signal itself when nothing upstream failed, was cancelled or gated.
    if aggregates and not (failures or counts["cancelled"] or counts["gated"]):
        failures.extend(aggregates)
        counts["failure"] += len(aggregates)
    return {"counts": counts, "failures": failures, "pending": pending,
            "cancelled": cancelled, "gated": gated, "aggregates_failed": len(aggregates),
            "total": sum(counts.values())}


LOG_MISSING = re.compile(r"\(?(not a GitHub Actions job|log too large|log fetch failed|empty log|not fetched)\b"
                         r"|log not found|no failure log", re.I)


def log_missing(text):
    """True for the notes failed_job_log returns instead of a log, wherever
    they appear (an agent may quote them back)."""
    return bool(LOG_MISSING.search(text or "")) and len((text or "").strip().splitlines()) <= 2


def failed_job_log(url, max_lines=250):
    """Tail of a failed Actions job log, or a short note saying why there is none."""
    match = JOB_URL.search(url or "")
    if not match:
        return "(not a GitHub Actions job, no log)"
    try:
        out = gh(["run", "view", "--repo", match.group(1), "--job", match.group(3), "--log-failed"],
                 timeout=300)
    except subprocess.TimeoutExpired:
        return "(log too large, not downloaded within 300 s)"
    except GitHubError as exc:
        return f"(log fetch failed: {str(exc)[:200]})"
    return "\n".join(out.splitlines()[-max_lines:]) or "(empty log)"


RERUN_MARKER = re.compile(r"<!--rrt:\d+(?::done)?-->")
RUN_URL = re.compile(r"\((https://github\.com/[^)]+/actions/runs/\d+)\)")


def rerun_test_status(repo, number, since_iso):
    """Result of the latest /rerun-test dispatch posted after `since_iso`.

    The slash handler replies with one line per runner batch, e.g.
    "🚀 `1-gpu-h100` (1 test): ⏳ [View workflow run](...) <!--rrt:0-->";
    the run later edits it to ✅ or ❌ and the marker to `<!--rrt:0:done-->`."""
    comments = gh_json(["api", "-X", "GET", f"repos/{repo}/issues/{number}/comments",
                        "-f", f"since={since_iso}", "-f", "per_page=100"]) or []
    for item in reversed(comments):
        body = item.get("body") or ""
        lines = [line for line in body.splitlines() if RERUN_MARKER.search(line) or line.startswith("⛔")]
        if not lines:
            continue
        runs = [m.group(1) for line in lines for m in [RUN_URL.search(line)] if m]
        if any("❌" in line or line.startswith("⛔") for line in lines):
            state = "failed"
        elif all("✅" in line for line in lines):
            state = "passed"
        else:
            state = "running"
        # the bot edits its reply when the run ends, so updated_at is when it finished
        return {"state": state, "url": item.get("html_url"), "runs": runs, "updated_at": item.get("updated_at", "")}
    return {"state": "missing", "url": "", "runs": []}


def approved_heads(repo, number, login):
    """Commits `login` has approved on this PR (REST keeps commit_id per review)."""
    reviews = gh_json(["api", "-X", "GET", f"repos/{repo}/pulls/{number}/reviews", "-f", "per_page=100"]) or []
    return {r.get("commit_id") for r in reviews
            if (r.get("user") or {}).get("login") == login and r.get("state") == "APPROVED"}


RUN_ID = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/actions/runs/(\d+)")


def run_failed_jobs(run_url):
    """Failed jobs of one workflow run (used for targeted /rerun-test runs)."""
    match = RUN_ID.search(run_url or "")
    if not match:
        return []
    repo, run_id = match.groups()
    data = gh_json(["run", "view", run_id, "--repo", repo, "--json", "jobs"]) or {}
    return [{"name": job["name"], "conclusion": (job.get("conclusion") or "").upper(),
             "url": job.get("url") or f"https://github.com/{repo}/actions/runs/{run_id}/job/{job.get('databaseId')}",
             "completed_at": job.get("completedAt") or ""}
            for job in data.get("jobs", []) if job.get("conclusion") in ("failure", "timed_out", "cancelled")]


def runs_awaiting_approval(repo, head):
    """Workflow runs of this commit that GitHub holds until a maintainer approves
    them (fork PRs from first-time contributors, bot branches)."""
    runs = gh_json(["run", "list", "--repo", repo, "--commit", head, "--status", "action_required",
                    "--limit", "50", "--json", "databaseId,workflowName,url"]) or []
    return [{"id": run["databaseId"], "workflow": run.get("workflowName", ""), "url": run.get("url", "")}
            for run in runs]


UNLINKED_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    commits(last: 100) { nodes { commit { oid author { user { login } } } } } } } }"""


def unlinked_commits(repo, number):
    """Commits whose author is not linked to any GitHub account. The repo's
    rules ask for extra approval for such "unattributed" changes, so GitHub
    blocks the merge. Only the primary author counts: Co-authored-by trailers
    (e.g. an AI assistant) are never linked and do not matter here."""
    owner, name = repo.split("/")
    data = gh_json(["api", "graphql", "-f", f"query={UNLINKED_QUERY}", "-F", f"owner={owner}",
                    "-F", f"name={name}", "-F", f"number={number}"]) or {}
    nodes = ((((data.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
             .get("commits") or {}).get("nodes") or []
    return [n["commit"]["oid"][:12] for n in nodes if not ((n["commit"].get("author") or {}).get("user"))]


def push_target(repo, number):
    """Where a maintainer may push to a PR branch, or None when the author did
    not allow edits from maintainers."""
    data = gh_json(["pr", "view", str(number), "--repo", repo, "--json",
                    "maintainerCanModify,isCrossRepository,headRepositoryOwner,headRepository,headRefName,state"])
    if data.get("state") != "OPEN" or not data.get("headRepository"):
        return None
    if data.get("isCrossRepository") and not data.get("maintainerCanModify"):
        return None
    owner, name = data["headRepositoryOwner"]["login"], data["headRepository"]["name"]
    return {"url": f"https://github.com/{owner}/{name}.git", "branch": data["headRefName"],
            "fork": f"{owner}/{name}"}


def discussion(repo, number, since_iso):
    """The PR conversation since `since_iso`, oldest first: review-thread
    comments (with the thread's first comment as `in_reply_to`) and plain PR
    comments."""
    def rows(path, kind):
        data = gh_json(["api", "-X", "GET", path, "-f", f"since={since_iso}", "-f", "per_page=100"]) or []
        return [{"id": c["id"], "kind": kind, "author": (c.get("user") or {}).get("login", ""),
                 "body": c.get("body") or "", "url": c.get("html_url", ""), "created_at": c.get("created_at", ""),
                 "in_reply_to": c.get("in_reply_to_id"), "path": c.get("path"),
                 "line": c.get("line") or c.get("original_line")} for c in data]
    items = rows(f"repos/{repo}/pulls/{number}/comments", "review") + rows(f"repos/{repo}/issues/{number}/comments", "issue")
    return sorted(items, key=lambda c: c["created_at"])


def reply_in_thread(repo, number, comment_id, body):
    out = gh(["api", "-X", "POST", f"repos/{repo}/pulls/{number}/comments/{comment_id}/replies",
              "-f", f"body={body}", "--jq", ".html_url"], write=True)
    return out.strip()


def search_issues(query, limit=100):
    data = gh_json(["api", "-X", "GET", "search/issues", "-f", f"q={query}",
                    "-f", f"per_page={min(limit, 100)}", "-f", "sort=updated"])
    return data.get("items", []) if data else []


# ---- writes (orchestrator only) ------------------------------------------

def comment(repo, number, body):
    out = gh(["pr", "comment", str(number), "--repo", repo, "--body-file", "-"], input_text=body, write=True)
    return out.strip()


def merge(repo, number, head, method="squash"):
    """Squash-merges at exactly `head`; GitHub refuses if the head moved."""
    gh(["pr", "merge", str(number), "--repo", repo, f"--{method}", "--match-head-commit", head], write=True)


def approve_run(repo, run_id):
    gh(["api", "-X", "POST", f"repos/{repo}/actions/runs/{run_id}/approve"], write=True)


def submit_review(repo, number, commit_id, event, body, comments=()):
    """One batched review pinned to commit_id, so a stale head is rejected."""
    payload = {"commit_id": commit_id, "event": event, "body": body,
               "comments": [c for c in comments if c.get("path") and c.get("line")]}
    data = gh_json(["api", "-X", "POST", f"repos/{repo}/pulls/{number}/reviews", "--input", "-"],
                   input_text=json.dumps(payload), write=True)
    return {"id": data.get("id"), "url": data.get("html_url")}
