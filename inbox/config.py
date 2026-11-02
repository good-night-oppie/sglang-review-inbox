"""Configuration: defaults merged with a user JSON file. No secrets live here."""

import copy
import json
import os
import re
import subprocess
from pathlib import Path

PLACEHOLDER = re.compile(r"<[A-Z][A-Z0-9_]*>")

DEFAULTS = {
    # The GitHub account the agent acts as. Empty = whoever `gh` is logged in as.
    "github_login": "",
    # Only PRs in these repos are ever reviewed or touched.
    "repos": ["sgl-project/sglang"],
    # Review PRs opened by github_login itself (a team sharing one account).
    "review_own_prs": False,
    "data_dir": "~/.cache/review-inbox",
    # shadow: agents review and track, but nothing is written to GitHub/Slack
    # unless you click a button. live: the autonomous writes allowed by
    # `policy` below are performed.
    "mode": "shadow",
    "sources": {
        "slack": {
            "enabled": True,
            # User tokens (xoxp-), one per workspace: from this env var
            # (comma-separated) or from token_file (one per line, mode 0600).
            "token_env": "SLACK_USER_TOKEN",
            "token_file": "~/.cache/review-inbox/slack-tokens",
            "poll_seconds": 120,
            # How far back @-mentions are collected for the Slack view.
            "lookback_days": 14,
            # Review requests to you (mentions, DMs, threads) younger than this
            # are handled; older ones are listed for you to hand over by hand.
            "review_max_age_days": 14,
            # PRs posted in watch_channels without an @ are picked up this fresh.
            "watch_max_age_days": 3,
            # `{uid}` is replaced with your Slack user id.
            "queries": ["<@{uid}>"],
            # Fetch the thread parent when a mention has no PR link itself.
            "follow_threads": True,
            # The PR link may come from the thread parent, but the ask itself
            # must be in the message that @-mentions you.
            # No \b before English words: "幫忙review一下" has no word boundary.
            "review_intent": (r"(?<![a-z])(review|ptal|approve|stamp|lgtm\?)(?![a-z])|take a look|have a look|"
                              r"another look|look into|(can|could|would) (you|someone|anyone) "
                              r"(please |pls |kindly |help )*(merge|land|approve|stamp|trigger|run|add|rerun)|"
                              r"please (merge|approve|trigger|land)|merge (it|this)|trigger (the )?ci|run[- ]?ci|"
                              r"tag-and-rerun|rerun|帮忙|幫忙|帮看|幫看|看下|看一下|看看|审一下|審一下|审核|"
                              r"合一下|合入|合并一下|合併|麻烦|麻煩"),
            # A mention that is only a cc/fyi is listed apart and not counted as owed.
            "fyi_pattern": r"^\s*(cc|fyi|抄送)\b",
            # Channels where a posted PR is yours to review even without an
            # @-mention, unless the post @-mentions someone else or someone
            # other than the author already reviewed it on GitHub.
            "watch_channels": [],
        },
        # Your unhandled Slack messages for the dashboard (never auto-answered).
        # DMs: those with recent messages to you (found by search) plus the
        # `dm_conversations` Slack ranks highest.
        "slack_inbox": {"enabled": True, "poll_seconds": 300, "lookback_days": 14, "dm_conversations": 15},
        "github_mentions": {"enabled": True, "poll_seconds": 600, "lookback_days": 3},
        "github_assigned": {"enabled": True, "poll_seconds": 600, "lookback_days": 7},
        # CODEOWNERS makes this huge (1000+ open PRs); keep it off unless filtered.
        "github_review_requested": {
            "enabled": False,
            "poll_seconds": 1800,
            "lookback_days": 2,
        },
    },
    "agent": {
        # codex | claude. Model empty = the runner's own default
        # (~/.codex/config.toml, or Claude Code's default model).
        "runner": "codex",
        "model": "",
        # Per job: review tiers T1/T2/T3, "verify" and "ci". Each may set
        # "model" and "effort" (codex model_reasoning_effort); "" or missing
        # falls back to `model` above / the runner's own config.
        "profiles": {},
        # CODEX_HOME directories (one codex login each) in order of preference;
        # empty = the CLI default (~/.codex).
        "codex_homes": [],
        # codex service tier: "priority" is the Fast tier (faster, more usage).
        "service_tier": "",
        "max_concurrent": 2,
        "daily_review_limit": 40,
        # Agent (codex) work runs only inside these local-time windows, each
        # `window_minutes` long; [] = any time. Everything that costs no tokens
        # (polling, CI commands, merges, Slack replies) runs all the time.
        "windows": [],
        "window_minutes": 120,
        # Hard cap on codex tokens per local day (0 = none). Each running job
        # holds `token_reserve` until its real count is in.
        "daily_token_budget": 0,
        "token_reserve": 150000,
        "max_budget_usd": 10,
        "timeout_seconds": 2700,
        # Every review uses this skill at the latest commit of `branch`
        # (synced into data_dir/skills-repo); a run that skips it is rejected.
        "review_skill": {
            "repo": "https://github.com/BBuf/AI-Infra-Auto-Driven-SKILLS.git",
            "branch": "main",
            "subdir": "skills/sglang-humanize-review",
            "seed": "",  # optional local clone to borrow objects from on the first clone
            "sync_seconds": 600,
            "max_stale_seconds": 3600,
        },
        # Used only when review_skill.repo is empty.
        "review_skill_dir": "~/.claude/skills/sglang-humanize-review",
    },
    "policy": {
        # Autonomous writes, effective only in live mode.
        "auto_publish_findings": False,
        "auto_ci": True,
        "auto_targeted_tests": True,
        "auto_retry_failed_ci": True,
        "retry_per_failure_signature": 1,
        # Approve when the verified review has no blocking finding, current-head
        # CI passes under the merge bar, and targeted tests (if any) passed.
        "auto_approve": False,
        "auto_approve_tiers": ["T1", "T2"],
        "max_auto_approvals_per_day": 30,
        # Instead of waiting on the author, push the fix yourself (as you, to
        # branches that allow maintainer edits, never forced): merge conflicts,
        # verified blocking findings, CI failures caused by the change.
        "auto_fix": False,
        # Answer replies to the agent's review comments: concede, hold, or (rarely)
        # hand it to you.
        "auto_discuss": False,
        "max_agent_fixes_per_pr": 2,
        "max_agent_fixes_per_day": 20,
        # One fix runs at a time, and pushes are at least this far apart.
        "min_fix_interval_minutes": 15,
        # Squash-merge PRs that meet the merge bar (approved, CI passed on the
        # current head, GitHub review decision APPROVED, no WIP marker).
        "auto_merge": False,
        "max_auto_merges_per_day": 10,
        # Auto-merges land in main at least this far apart.
        "min_merge_interval_minutes": 60,
        # Tell the author on GitHub why a PR is stuck (no test plan, related
        # CI failure), once per head.
        "auto_author_notes": False,
        # Slack thread replies to review requests. Only once the PR has a clear
        # outcome, one short line each; no acks or progress chatter.
        "slack_report": False,
        "slack_outcomes": ["approved", "changes", "bounce", "ci_related", "merged"],
        # `{login}` becomes your GitHub login; the requester is @-mentioned after it.
        "slack_prefix": "[{login} Review Agent] ",
        # Every comment, review and reply the agent posts on GitHub starts with
        # this (CI slash commands excepted, the bots need them bare).
        "github_prefix": "[{login} Review Agent] ",
        # Readiness gates applied before any agent time is spent.
        "max_changed_lines": 2500,
        "require_test_plan": True,
        "require_perf_numbers": True,
        # PR text matching these never gets agent output posted anywhere.
        "confidential_keywords": [],
    },
    "refresh_seconds": {"active": 300, "waiting_author": 600},
    "server": {"host": "127.0.0.1", "port": 8765},
}


def _merge(base, override):
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _placeholders(value, where=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _placeholders(item, f"{where}.{key}" if where else key)
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from _placeholders(item, f"{where}[{i}]")
    elif isinstance(value, str) and PLACEHOLDER.search(value):
        yield where, value


def _gh_login():
    try:
        return subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=60, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("github_login is empty and `gh api user` failed; run `gh auth login` "
                         "or set github_login in config.json") from exc


def load(path=None):
    config = copy.deepcopy(DEFAULTS)
    if path and Path(path).exists():
        _merge(config, json.loads(Path(path).read_text()))
    left = list(_placeholders(config))
    if left:
        raise ValueError("config still has placeholders, see README (Configure): "
                         + ", ".join(f"{where}={value}" for where, value in left))
    if not config["github_login"]:
        config["github_login"] = _gh_login()
    for home in config["agent"].get("codex_homes") or []:
        if not (Path(home).expanduser() / "auth.json").exists():
            raise ValueError(f"agent.codex_homes: {home} has no codex login; run "
                             f"`CODEX_HOME={home} codex login` (see README, Codex accounts)")
    config["data_dir"] = str(Path(config["data_dir"]).expanduser())
    slack = config["sources"]["slack"]
    slack["token_file"] = str(Path(slack["token_file"]).expanduser())
    agent = config["agent"]
    agent["review_skill_dir"] = str(Path(agent["review_skill_dir"]).expanduser())
    Path(config["data_dir"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return config


def slack_tokens(config):
    slack = config["sources"]["slack"]
    tokens = [t.strip() for t in os.environ.get(slack["token_env"], "").split(",")]
    path = Path(slack["token_file"])
    if path.exists():
        tokens += [line.strip() for line in path.read_text().splitlines()
                   if line.strip() and not line.lstrip().startswith("#")]
    return list(dict.fromkeys(t for t in tokens if t))


def is_live(config):
    return config.get("mode") == "live"
