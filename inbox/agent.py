"""Headless review agents (Codex or Claude Code) in a read-only sandbox.

Agents get a detached worktree of the PR head plus a context bundle written
by the orchestrator. They have no GitHub/Slack credentials or network, and
run from a clean directory so the PR cannot inject settings, hooks or
AGENTS.md/CLAUDE.md into the agent."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from . import github
from .skill import missing_skill_steps

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = ROOT / "prompts"


def _strict(properties, required=None):
    """Object schema accepted by both Codex structured output and Claude."""
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": required or list(properties)}


STR = {"type": "string"}
STR_LIST = {"type": "array", "items": STR}

REVIEW_SCHEMA = _strict({
    "summary": STR,
    "mermaid": STR,
    "corpus_synthesis": STR,
    "verdict": {"type": "string", "enum": ["lgtm", "minor", "changes_needed", "needs_human"]},
    "verdict_reason": STR,
    "findings": {"type": "array", "items": _strict({
        "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
        "category": STR,
        "path": STR,
        "line": {"type": "integer"},
        "title": STR,
        "scenario": STR,
        "evidence": STR,
        "fix": STR,
        "comment": STR,
        "raised_before": {"type": "boolean"},  # repeats an earlier, still unresolved review point
        "kind": {"type": "string", "enum": ["bug", "evidence"]},
    })},
    "test_gaps": STR_LIST,
    "related_tests": STR_LIST,
    "targeted_tests": STR_LIST,
    "approve_note": STR,
    "coverage_notes": STR,
})

VERIFY_SCHEMA = _strict({
    "verdicts": {"type": "array", "items": _strict({
        "index": {"type": "integer"},
        "verdict": {"type": "string", "enum": ["confirmed", "plausible", "rejected"]},
        "reason": STR,
    })},
})

CI_SCHEMA = _strict({
    "failures": {"type": "array", "items": _strict({
        "name": STR,
        "classification": {"type": "string",
                           "enum": ["related", "infra", "flaky", "unrelated", "unknown"]},
        "evidence": STR,
    })},
    "summary": STR,
})

TIER_HINTS = {
    "T1": "Low-risk change. One careful pass; keep findings to real problems.",
    "T2": "Normal change. Review each hunt category in turn before concluding.",
    "T3": ("High-risk change (hot path, defaults/API, large kernel or model). "
           "Review each hunt category separately and trace the changed code "
           "through its callers. Use verdict needs_human for design questions."),
}


class AgentError(RuntimeError):
    pass


def render(template_name, **values):
    text = (PROMPTS / template_name).read_text()
    for name, value in values.items():
        text = text.replace("{" + name + "}", str(value))
    return text


def _git(repo_dir, *args, timeout=600):
    proc = subprocess.run(["git", "-C", str(repo_dir), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise AgentError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[:400]}")
    return proc.stdout


# A follow-up review reads only what changed since the reviewed head; past
# this many changed lines it is a rewrite and gets a full review.
DELTA_MAX_LINES = 1500


def fingerprint(diff_text):
    """What a PR changes, independent of where main is: blob ids and hunk
    offsets dropped, so merging main alone leaves it the same."""
    kept = ["@@" if line.startswith("@@") else line for line in diff_text.splitlines()
            if not line.startswith(("index ", "similarity index", "dissimilarity index"))]
    return hashlib.sha1("\n".join(kept).encode()).hexdigest()


class Workspace:
    """A detached worktree of one PR head plus its context bundle."""

    def __init__(self, config, repo, number, head, base_ref):
        self.data = Path(config["data_dir"])
        self.clone_url = config.get("git_url", "https://github.com/{repo}.git").format(repo=repo)
        self.repo, self.number, self.head, self.base_ref = repo, number, head, base_ref
        self.mirror = self.data / "repos" / repo.replace("/", "__")
        self.worktree = self.data / "worktrees" / f"{repo.replace('/', '__')}-{number}-{head[:12]}"
        self.ctx = self.worktree.parent / (self.worktree.name + ".ctx")

    def _fetch(self):
        if not (self.mirror / ".git").exists():
            shutil.rmtree(self.mirror, ignore_errors=True)
            self.mirror.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout",
                                   self.clone_url, str(self.mirror)],
                                  capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
            if proc.returncode != 0:
                shutil.rmtree(self.mirror, ignore_errors=True)
                raise AgentError(f"git clone: {proc.stderr.strip()[-400:]}")
        _git(self.mirror, "fetch", "--quiet", "origin", self.base_ref,
             f"+refs/pull/{self.number}/head:refs/review-inbox/pr-{self.number}", timeout=1800)
        _git(self.mirror, "cat-file", "-e", f"{self.head}^{{commit}}")

    def diff_fingerprint(self):
        """Fingerprint of the PR's own change at this head, without a checkout."""
        self._fetch()
        merge_base = _git(self.mirror, "merge-base", f"origin/{self.base_ref}", self.head).strip()
        return fingerprint(_git(self.mirror, "diff", "--no-color", f"{merge_base}..{self.head}"))

    def prepare_delta(self, since):
        """Write what changed since an earlier reviewed head to delta.patch,
        limited to the files the PR touches. None when that head is gone
        (force-pushed away) or the delta is too big to call a follow-up."""
        try:
            _git(self.mirror, "cat-file", "-e", f"{since}^{{commit}}")
        except AgentError:
            try:
                _git(self.mirror, "fetch", "--quiet", "origin", since, timeout=600)
            except AgentError:
                return None
        files = _git(self.mirror, "diff", "--name-only", f"{self.merge_base}..{self.head}").split()
        if not files:
            return None
        delta = _git(self.mirror, "diff", "--no-color", f"{since}..{self.head}", "--", *files)
        changed = sum(1 for line in delta.splitlines()
                      if line[:1] in "+-" and not line.startswith(("+++", "---")))
        if changed > DELTA_MAX_LINES:
            return None
        (self.ctx / "delta.patch").write_text(delta)
        return delta

    def prepare(self, pr_snapshot):
        self._fetch()
        if self.worktree.exists() and not self._worktree_intact():
            self.cleanup()  # an interrupted checkout would hand the agent missing files
        if not self.worktree.exists():
            _git(self.mirror, "worktree", "add", "--detach", str(self.worktree), self.head, timeout=3600)
            if not self._worktree_intact():
                raise AgentError("worktree checkout incomplete")
        merge_base = _git(self.mirror, "merge-base", f"origin/{self.base_ref}", self.head).strip()
        self.merge_base = merge_base
        self.ctx.mkdir(parents=True, exist_ok=True)
        diff = _git(self.mirror, "diff", "--no-color", f"{merge_base}..{self.head}")
        (self.ctx / "diff.patch").write_text(diff)
        self.fingerprint = fingerprint(diff)
        (self.ctx / "pr.json").write_text(json.dumps(
            _pr_context(self.repo, self.number, pr_snapshot, merge_base), ensure_ascii=False, indent=1))
        return merge_base

    def _worktree_intact(self):
        try:
            return (_git(self.worktree, "rev-parse", "HEAD").strip() == self.head
                    and not _git(self.worktree, "status", "--porcelain", "--untracked-files=no").strip())
        except AgentError:
            return False

    def cleanup(self):
        if self.worktree.exists():
            # Double --force also removes a worktree left locked by an interrupted add.
            subprocess.run(["git", "-C", str(self.mirror), "worktree", "remove", "--force", "--force",
                            str(self.worktree)], capture_output=True)
            shutil.rmtree(self.worktree, ignore_errors=True)
            subprocess.run(["git", "-C", str(self.mirror), "worktree", "prune"], capture_output=True)
        shutil.rmtree(self.ctx, ignore_errors=True)


def _pr_context(repo, number, snapshot, merge_base):
    try:
        inline = github.gh_json(["api", f"repos/{repo}/pulls/{number}/comments?per_page=100"]) or []
    except github.GitHubError:
        inline = []
    return {
        "repo": repo, "number": number, "title": snapshot.get("title"),
        "author": (snapshot.get("author") or {}).get("login"),
        "head": snapshot.get("headRefOid"), "merge_base": merge_base,
        "labels": [label["name"] for label in snapshot.get("labels") or []],
        "body": (snapshot.get("body") or "")[:20000],
        "files": [{k: f.get(k) for k in ("path", "additions", "deletions")}
                  for f in snapshot.get("files") or []],
        "reviews": [{"author": (r.get("author") or {}).get("login"), "state": r.get("state"),
                     "body": (r.get("body") or "")[:1500]} for r in snapshot.get("latestReviews") or []],
        "inline_comments": [{"author": (c.get("user") or {}).get("login"), "path": c.get("path"),
                             "line": c.get("line"), "body": (c.get("body") or "")[:800]}
                            for c in inline[-60:]],
    }


# ---- codex accounts --------------------------------------------------------
# agent.codex_homes lists CODEX_HOME directories, one login each, in order of
# preference. A job runs on the first account that is not resting; one that
# hits its usage limit or spend cap rests for a while and the job moves on.
LIMIT_HIT = re.compile(r"spend cap|usage limit|insufficient_quota|exceeded your current quota", re.I)
ACCOUNT_REST = 30 * 60
_resting = {}
_resting_lock = threading.Lock()
_emails = {}
# Every codex run reports its token count here (the pipeline meters the daily budget).
TOKENS_USED = re.compile(r"tokens used\s*\n\s*([\d,]+)")
usage_hook = None


def tokens_used(stderr):
    found = TOKENS_USED.findall(stderr or "")
    return int(found[-1].replace(",", "")) if found else 0


def codex_homes(config):
    return [str(Path(home).expanduser()) for home in config["agent"].get("codex_homes") or []] or [None]


def account_email(home):
    """The login's email from its id_token (no secrets read beyond that)."""
    if home not in _emails:
        try:
            token = json.loads((Path(home or Path.home() / ".codex") / "auth.json").read_text())["tokens"]["id_token"]
            part = token.split(".")[1]
            _emails[home] = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email", "")
        except (OSError, KeyError, ValueError, IndexError):
            _emails[home] = ""
    return _emails[home] or (home or "default")



def _run_tree(cmd, *, timeout, **kw):
    """subprocess.run that, on timeout, kills the whole process tree (codex starts
    helper processes of its own; plain run() would only kill the top one on Windows)."""
    if os.name == "nt":
        kw.setdefault("creationflags", subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw.setdefault("start_new_session", True)
    stdin = kw.pop("input", None)
    with subprocess.Popen(cmd, stdin=subprocess.PIPE if stdin is not None else None,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw) as proc:
        try:
            out, err = proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            raise
        return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _kill_tree(proc):
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        import signal
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    proc.kill()

def run_codex(config, cmd, prompt, cwd, label):
    """Run `codex exec`, falling back to the next account on a usage limit or
    spend cap. Returns (process, account email)."""
    homes = codex_homes(config)
    with _resting_lock:
        ready = [home for home in homes if _resting.get(home, 0) <= time.time()] or homes
    proc = None
    for home in ready:
        env = dict(os.environ, CODEX_HOME=home) if home else None
        try:
            proc = _run_tree(cmd, input=prompt, text=True, encoding="utf-8", errors="replace", cwd=cwd,
                             timeout=config["agent"]["timeout_seconds"], env=env)
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"{label}: timed out after {config['agent']['timeout_seconds']}s") from exc
        if usage_hook:
            usage_hook(label, tokens_used(proc.stderr))
        if proc.returncode != 0 and LIMIT_HIT.search(proc.stderr or ""):
            with _resting_lock:
                _resting[home] = time.time() + ACCOUNT_REST
            continue
        with _resting_lock:
            _resting.pop(home, None)
        return proc, account_email(home)
    return proc, account_email(ready[-1])


def profile_for(config, job):
    agent = config["agent"]
    profile = dict(agent.get("profiles", {}).get(job) or {})
    profile["model"] = profile.get("model") or agent.get("model") or ""
    return profile


def run_agent(config, prompt, schema, workspace, label, profile=None, skill_dir=None):
    """Run one headless agent; returns (structured_result, run_info)."""
    agent = config["agent"]
    profile = profile or profile_for(config, label)
    run_dir = Path(config["data_dir"]) / "runs" / (
        f"{workspace.repo.replace('/', '__')}-{workspace.number}-{workspace.head[:8]}-{label}-{int(time.time())}")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(prompt)
    schema_path = run_dir / "schema.json"
    schema_path.write_text(json.dumps(schema))
    out_path = run_dir / "result.json"
    skill = str(skill_dir or agent["review_skill_dir"])

    if agent["runner"] == "codex":
        # --disable hooks: your interactive Stop/PostToolUse hooks (e.g. RLCR
        # loops) must not steer an unattended review or its final JSON.
        cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
               "--disable", "hooks",
               "-C", str(run_dir), "--add-dir", str(workspace.worktree), "--add-dir", str(workspace.ctx),
               "--add-dir", skill, "--output-schema", str(schema_path), "-o", str(out_path)]
        if profile.get("model"):
            cmd += ["-m", profile["model"]]
        if profile.get("effort"):
            cmd += ["-c", f'model_reasoning_effort="{profile["effort"]}"']
        if agent.get("service_tier"):
            cmd += ["-c", f'service_tier="{agent["service_tier"]}"']
        cmd.append("-")
    else:
        allowed = ["Read", "Grep", "Glob", "Agent", "Bash(git diff:*)", "Bash(git log:*)",
                   "Bash(git show:*)", "Bash(git blame:*)", "Bash(git grep:*)",
                   f"Bash(python3 {skill}/scripts/summarize_sglang_review_corpus.py:*)",
                   f"Bash(python3 {skill}/scripts/query_sglang_review_corpus.py:*)"]
        cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
               "--permission-mode", "dontAsk", "--tools", "Read,Grep,Glob,Bash,Agent",
               "--allowedTools", *allowed,
               "--disallowedTools", "Bash(gh:*)", "Bash(curl:*)", "Bash(wget:*)", "Bash(git push:*)",
               "--max-budget-usd", str(agent["max_budget_usd"]),
               "--add-dir", str(workspace.worktree), str(workspace.ctx), skill]
        if profile.get("model"):
            cmd += ["--model", profile["model"]]

    # Windows: npm installs codex/claude as .cmd shims; CreateProcess will not resolve the bare name.
    cmd[0] = shutil.which(cmd[0]) or cmd[0]
    if os.name == "nt" and agent["runner"] == "codex":
        # Windows: codex's read-only sandbox only reads under the working directory (--add-dir is not
        # honoured for reads), so run from data_dir, which holds runs/, worktrees/ and the skill copy.
        root = str(Path(config["data_dir"]).expanduser())
        i = cmd.index("-C")
        cmd[i + 1] = root
    started = time.time()
    account = ""
    if agent["runner"] == "codex":
        proc, account = run_codex(config, cmd, prompt, run_dir, label)
    else:
        try:
            proc = _run_tree(cmd, input=prompt, text=True, encoding="utf-8", errors="replace",
                             cwd=run_dir, timeout=agent["timeout_seconds"])
        except subprocess.TimeoutExpired as exc:
            raise AgentError(f"{label}: timed out after {agent['timeout_seconds']}s") from exc
    (run_dir / "stdout.txt").write_text(proc.stdout or "")
    (run_dir / "stderr.txt").write_text(proc.stderr or "")
    info = {"runner": agent["runner"], "label": label, "seconds": round(time.time() - started),
            "model": profile.get("model") or "default", "effort": profile.get("effort") or "default",
            "account": account, "run_dir": str(run_dir)}

    if agent["runner"] == "codex":
        if proc.returncode != 0 or not out_path.exists():
            raise AgentError(f"{label}: codex exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
        return _parse_json(out_path.read_text(), label), info

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise AgentError(f"{label}: claude exit {proc.returncode}: {proc.stderr.strip()[-300:]}") from exc
    if envelope.get("is_error"):
        raise AgentError(f"{label}: {str(envelope.get('result'))[:300]}")
    info["cost_usd"] = envelope.get("total_cost_usd")
    result = envelope.get("structured_output") or _parse_json(envelope.get("result") or "", label)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result, info


def _parse_json(text, label):
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    try:
        return json.loads(fenced.group(1) if fenced else text)
    except (json.JSONDecodeError, AttributeError) as exc:
        raise AgentError(f"{label}: unparseable agent output") from exc


# ---- the three agent jobs --------------------------------------------------

def review(config, workspace, tier, skill_dir, skill_commit=""):
    prompt = render("review.md", repo=workspace.repo, number=workspace.number, head=workspace.head,
                    login=config["github_login"], worktree=workspace.worktree, ctx_dir=workspace.ctx,
                    skill_dir=skill_dir, skill_commit=skill_commit[:12] or "local", tier=tier,
                    tier_hint=TIER_HINTS.get(tier, TIER_HINTS["T2"]))
    result, info = run_agent(config, prompt, REVIEW_SCHEMA, workspace, "review", profile_for(config, tier),
                             skill_dir=skill_dir)
    info["skill_commit"] = skill_commit
    if config["agent"]["runner"] == "codex":
        # Mandatory: a review that did not follow the latest skill is rejected and retried.
        missing = missing_skill_steps((Path(info["run_dir"]) / "stderr.txt").read_text(), skill_dir)
        if missing:
            raise AgentError("review did not follow the sglang-humanize-review skill: " + ", ".join(missing))
    return result, info


def review_delta(config, workspace, tier, skill_dir, since, previous):
    """Follow-up review of a new head: only the change since `since`, plus
    whether the previous findings still hold. No corpus sweep."""
    prompt = render("review_delta.md", repo=workspace.repo, number=workspace.number, head=workspace.head,
                    since=since, login=config["github_login"], worktree=workspace.worktree,
                    ctx_dir=workspace.ctx, skill_dir=skill_dir, tier=tier,
                    previous=json.dumps(previous, ensure_ascii=False, indent=1))
    result, info = run_agent(config, prompt, REVIEW_SCHEMA, workspace, "delta", profile_for(config, tier),
                             skill_dir=skill_dir)
    info["delta_since"] = since
    return result, info


BLOCKING = ("blocker", "major")


def verify(config, workspace, findings):
    """Only blocker/major findings decide the verdict, so only they are
    re-checked; minor and nit come back as "unverified" and never block."""
    blocking = [i for i, finding in enumerate(findings) if finding["severity"] in BLOCKING]
    unverified = [{"index": i, "verdict": "unverified", "reason": "non-blocking, not verified"}
                  for i in range(len(findings)) if i not in blocking]
    if not blocking:
        return {"verdicts": unverified}, {"label": "verify", "skipped": True}
    subset = [dict(findings[i], index=n) for n, i in enumerate(blocking)]
    prompt = render("verify.md", repo=workspace.repo, number=workspace.number, head=workspace.head,
                    worktree=workspace.worktree, ctx_dir=workspace.ctx,
                    findings_json=json.dumps(subset, ensure_ascii=False, indent=1))
    result, info = run_agent(config, prompt, VERIFY_SCHEMA, workspace, "verify")
    verdicts = [dict(v, index=blocking[v["index"]]) for v in result.get("verdicts", [])
                if 0 <= v["index"] < len(blocking)]
    return {"verdicts": verdicts + unverified}, info


def triage_ci(config, workspace, failures, related_tests):
    (workspace.ctx / "ci_failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=1))
    prompt = render("ci_triage.md", repo=workspace.repo, number=workspace.number, head=workspace.head,
                    worktree=workspace.worktree, ctx_dir=workspace.ctx,
                    related_tests=", ".join(related_tests) or "(none listed)")
    return run_agent(config, prompt, CI_SCHEMA, workspace, "ci")


DISCUSS_SCHEMA = _strict({
    "answers": {"type": "array", "items": _strict({
        "comment_id": STR,
        "decision": {"type": "string", "enum": ["concede", "hold", "maintainer", "approve", "none"]},
        "about": {"type": "string", "enum": ["finding", "ci", "other"]},
        "finding": STR,
        "reply": STR,
    })},
    "summary": STR,
})


def discuss(config, workspace, findings, threads):
    """Answer replies on review comments: concede, hold, or (rarely) leave it
    to the maintainer."""
    commits = _git(workspace.mirror, "log", "--no-color", "--format=%H parents:%P%n    %s", "--name-status",
                   f"{workspace.merge_base}..{workspace.head}")
    (workspace.ctx / "commits.txt").write_text(commits)
    prompt = render("discuss.md", repo=workspace.repo, number=workspace.number, head=workspace.head,
                    login=config["github_login"], worktree=workspace.worktree, ctx_dir=workspace.ctx,
                    findings=json.dumps(findings, ensure_ascii=False, indent=1),
                    threads=json.dumps(threads, ensure_ascii=False, indent=1))
    return run_agent(config, prompt, DISCUSS_SCHEMA, workspace, "discuss")


FIX_SCHEMA = _strict({
    "fixed": STR_LIST,
    "skipped": {"type": "array", "items": _strict({"id": STR, "reason": STR})},
    "summary": STR,
})


def fix(config, worktree, repo, number, task, issues):
    """Let the agent edit a PR checkout in place (workspace-write sandbox, no
    network). Git is not the agent's: the caller commits and pushes."""
    agent = config["agent"]
    if agent["runner"] != "codex":
        raise AgentError("agent fixes need the codex runner")
    profile = profile_for(config, "fix")
    run_dir = Path(config["data_dir"]) / "runs" / f"{repo.replace('/', '__')}-{number}-fix-{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt = render("fix.md", repo=repo, number=number, login=config["github_login"], worktree=worktree,
                    task=task, issues=json.dumps(issues, ensure_ascii=False, indent=1))
    (run_dir / "prompt.md").write_text(prompt)
    schema_path, out_path = run_dir / "schema.json", run_dir / "result.json"
    schema_path.write_text(json.dumps(FIX_SCHEMA))
    cmd = ["codex", "exec", "-s", "workspace-write", "--skip-git-repo-check", "--ephemeral",
           "--disable", "hooks", "-C", str(worktree), "--output-schema", str(schema_path), "-o", str(out_path)]
    if profile.get("model"):
        cmd += ["-m", profile["model"]]
    if profile.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{profile["effort"]}"']
    if agent.get("service_tier"):
        cmd += ["-c", f'service_tier="{agent["service_tier"]}"']
    cmd.append("-")
    proc, _ = run_codex(config, cmd, prompt, worktree, "fix")
    (run_dir / "stderr.txt").write_text(proc.stderr or "")
    if proc.returncode != 0 or not out_path.exists():
        raise AgentError(f"fix: codex exit {proc.returncode}: {proc.stderr.strip()[-300:]}")
    return _parse_json(out_path.read_text(), "fix")


TRANSLATE_SCHEMA = _strict({"items": {"type": "array", "items": _strict({"id": STR, "zh": STR})}})


def translate_zh(config, texts):
    """English agent text -> Simplified Chinese for the dashboard's Chinese
    view. Returns {english: chinese} for the items the model returned."""
    scratch = Path(config["data_dir"]) / "translate"
    scratch.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1("\n".join(texts).encode()).hexdigest()
    box = SimpleNamespace(repo="dashboard", number=0, head=digest, worktree=scratch, ctx=scratch)
    items = [{"id": str(i), "text": text} for i, text in enumerate(texts)]
    prompt = render("translate_zh.md", items=json.dumps(items, ensure_ascii=False, indent=1))
    result, _ = run_agent(config, prompt, TRANSLATE_SCHEMA, box, "translate")
    out = {}
    for item in result.get("items", []):
        index = item.get("id", "")
        if index.isdigit() and int(index) < len(texts) and item.get("zh", "").strip():
            out[texts[int(index)]] = item["zh"].strip()
    return out


def apply_verification(findings, verification):
    """Keep confirmed findings, keep plausible ones flagged low-confidence."""
    verdicts = {v["index"]: v for v in verification.get("verdicts", [])}
    kept, dropped = [], []
    for index, finding in enumerate(findings):
        verdict = verdicts.get(index, {"verdict": "plausible", "reason": "no verdict from the verifier"})
        finding = dict(finding, verify=verdict["verdict"], verify_reason=verdict["reason"])
        (dropped if verdict["verdict"] == "rejected" else kept).append(finding)
    return kept, dropped


# Reviews written before findings carried `kind`: these categories ask for
# proof (tests, benchmarks, validation), not a fix to a defect.
EVIDENCE_CATEGORY = re.compile(r"^(tests?|validation|hardware-validation)$|^tests?[/-]|evidence|benchmark|claim", re.I)


def is_risk(finding):
    """Only a concrete defect blocks a PR; asking for more tests, measurements
    or hardware validation never does."""
    if finding.get("kind"):
        return finding["kind"] == "bug"
    return not EVIDENCE_CATEGORY.search(finding.get("category") or "")


def effective_verdict(review_result, kept):
    """Recompute the verdict after the verifier drops findings."""
    blocking = [f for f in kept if f["severity"] in ("blocker", "major") and is_risk(f)]
    confirmed_blocking = [f for f in blocking if f["verify"] == "confirmed"]
    plausible_blocking = [f for f in blocking if f["verify"] == "plausible"]
    if confirmed_blocking:
        return "changes_needed"  # the author has concrete work first, whatever else is open
    if review_result["verdict"] == "needs_human" or plausible_blocking:
        return "needs_human"
    return "minor" if kept else "lgtm"
