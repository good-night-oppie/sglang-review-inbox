<div align="center">

<img src="web/assets/sgl-wordmark.png" alt="SGLang" height="56">

# SGLang Review Inbox

**Let coding agents carry PR reviews from "can you take a look?" to "merged", and keep for yourself only the PRs that need a maintainer.**

[![English](https://img.shields.io/badge/README-English-D55816?style=for-the-badge)](README.md)
[![简体中文](https://img.shields.io/badge/README-%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-A5300F?style=for-the-badge)](README.zh-CN.md)

</div>

Review Inbox is a small local web app for an SGLang maintainer who gets more review requests than they can read. It collects the PRs people ask you to review (Slack mentions, DMs, a PR channel, GitHub mentions and assignments) and runs the whole loop with coding agents: review, independent verification, CI, conversations with the author, small fixes, approval and merge. It reports back in the original Slack thread. A bilingual (English / 中文) dashboard in your browser shows where every PR stands. What still needs you is kept to the high-risk PRs.

It acts on GitHub and Slack **as you**, through your own `gh` login and your own Slack user token. It starts in a read-only *shadow* mode. Every write it can make is behind a policy switch, and each write happens at most once.

## What it does

- **Intake.** Slack @-mentions, DMs (including thread replies), follow-ups later in a thread that mentioned you, PRs posted in a team channel (e.g. `#pull-requests`) that nobody else was asked about, plus GitHub @-mentions and PRs assigned to you. Short "thanks / ok" messages and FYIs are not treated as asks.
- **Review.** A codex agent reviews each PR using the [sglang-humanize-review](https://github.com/BBuf/AI-Infra-Auto-Driven-SKILLS/tree/main/skills/sglang-humanize-review) skill, always at its latest commit. A second agent then tries to refute every blocking finding, and only verified findings are kept. **Only concrete bugs block a PR**: requests for more tests, benchmarks or hardware validation stay as non-blocking notes on the dashboard, and the same evidence is never asked for twice. PRs are sized into tiers: T1/T2 are approved automatically when clean, T3 (hot paths, large or risky changes) waits for you.
- **CI.** Sends `/tag-and-rerun-ci` and targeted `/rerun-test` runs, and retries with backoff. It approves held fork runs, triages failures from the logs and ignores hardware lanes the change does not touch. It tells the author, with the key error line, about failures that are theirs.
- **Conversation.** Reads every reply until the PR is merged. When an author pushes back on a finding, the agent checks the claim against the code and the commits. It then concedes, explains what is still needed, or (rarely) hands the decision to you. When **you** comment on the PR yourself ("ignore this", "LGTM", "please also fix X"), the agent takes it as the decision and carries on from there; you never have to come back to the dashboard.
- **Fixes (optional).** Pushes the fix itself instead of waiting on the author: merge conflicts, verified findings, CI failures caused by the change. Only to branches that allow maintainer edits, fast-forward only, never forced.
- **Merge (optional).** Squash-merges PRs that meet the merge bar and answers the Slack thread with one closing line.
- **Dashboard.** `http://127.0.0.1:8765`. Tabs for what needs you, what is on CI, what waits on the author, Slack messages you still owe a reply, and merged PRs. One click switches between English and 中文; agent-written text is translated in the background.

## How a PR moves

```
ask (Slack / GitHub) ─▶ triage ─▶ agent review ─▶ verifier
                                                    │
        ┌──────────────── changes needed ◀──────────┤
        │  agent fixes it, or posts the findings     │ clean
        │  and talks with the author until resolved  ▼
        └──────────────────────────────▶ approve (T1/T2) / Needs you (T3)
                                                    │
                     CI: run, retry, triage, ping ◀─┘
                                                    │
                          merge ─▶ "Merged …, thanks!" in the Slack thread
```

## Safety model

- **Shadow first.** In `shadow` mode the agents review and track but nothing is written anywhere until you click. Switch to `live` from the mode pill in the top bar; switching back is one click and takes effect immediately.
- **At most once.** Every external write (comment, approval, CI command, Slack reply, push, merge) is claimed in a ledger keyed by PR, head commit and action, so a restart or a retry never repeats it.
- **Guards on the big actions.** Daily caps on reviews, approvals, merges and fixes. No merge with a WIP / do-not-merge marker. Never `--admin`. Pushes are fast-forward only, so an author's newer push is never overwritten.
- **Local only.** The server listens on `127.0.0.1`. Every write request needs an anti-CSRF token embedded in the page. Slack tokens are stored in a `0600` file under the data directory and are never shown again.
- **Agents are sandboxed.** Reviews run `codex exec` read-only with no network; fixes run in a workspace-write sandbox on a throwaway checkout, and git is driven by the server, not the agent. PR text, comments and logs are treated as untrusted data in every prompt.

## Requirements

- macOS or Linux, **Python 3.11+** (standard library only, no `pip install`).
- [`gh`](https://cli.github.com/) logged in (`gh auth login`) as the maintainer account, with write access to the repos you review (approve, comment, labels, merge).
- `git`, and [`codex`](https://github.com/openai/codex) logged in with one or more accounts (see below).
- Optional: [`pre-commit`](https://pre-commit.com/), so agent fixes are formatted with the repo's own hooks.
- A Slack workspace where you can install an app (it only needs a **user** token).

## Deploy and open it in your browser

**1. Get the code and create your config**

```bash
git clone https://github.com/BBuf/sglang-review-inbox.git
cd sglang-review-inbox
cp config.example.json config.json
```

**2. Fill in the placeholders in `config.json`.** The server refuses to start while any `<...>` placeholder is left.

| Placeholder | What to put there |
| --- | --- |
| `<YOUR_GITHUB_LOGIN>` | Your GitHub login (the account `gh` is logged in as). An empty string also works: it is then read from `gh api user`. |
| `<PATH_TO_CODEX_HOME_1>`, `<PATH_TO_CODEX_HOME_2>` | Your codex accounts, see the next step. Keep one entry if you have one account. |
| `<CODEX_MODEL>`, `<CODEX_MODEL_STRONGEST>` | Model names your codex accounts can use, per job (`T1`/`T2`/`T3` reviews, `verify`, `ci`, `fix`, `discuss`, `translate`). Remove a profile to use the codex default model. |

`repos` lists the only repositories the agent may ever touch. `sources.slack.watch_channels` names channels where PRs are picked up without an @-mention; use `[]` to turn that off.

**3. Codex accounts (placeholders in the example).** Each entry in `agent.codex_homes` is a `CODEX_HOME` directory holding one codex login. They are tried in order. When one hits its usage limit or spend cap, the job moves to the next one and the first account rests for 30 minutes. Log each one in once:

```bash
CODEX_HOME=~/.codex-personal codex login   # first choice
CODEX_HOME=~/.codex codex login            # fallback (the CLI default home)
```

Then put those two paths into `codex_homes`. Do not commit `config.json`: it is in `.gitignore`. The app never reads codex credentials itself; it only sets `CODEX_HOME` for the `codex` process, and reads the account email from `auth.json` to show which account ran a job.

**4. Check and start it**

```bash
python3 -m unittest discover -s tests        # 80+ tests, no network needed
python3 server.py --config config.json
```

Open **http://127.0.0.1:8765** in your browser. Click **EN / 中文** in the top bar to switch the language.

**5. Connect Slack**

1. Open <https://api.slack.com/apps?new_app=1>, choose **From a manifest**, pick your workspace and paste [`slack-app-manifest.json`](slack-app-manifest.json). It asks only for user scopes: search, history and read access to your channels and DMs, user names, and `chat:write` for thread replies.
2. Install the app to the workspace and copy the **User OAuth Token** (`xoxp-…`) from *OAuth & Permissions*.
3. In the dashboard click **Connect Slack** and paste it. It is checked with `auth.test` and saved to `<data_dir>/slack-tokens` (mode `0600`). Repeat for each workspace.

**6. Go live.** Watch a few PRs in shadow mode first. When the reviews look right, click the mode pill to switch to live, then turn on the riskier switches in `policy` (`auto_merge`, `auto_fix`) when you are ready.

### Keep it running

```bash
nohup python3 server.py --config config.json > ~/.cache/review-inbox/server.log 2>&1 &
```

<details>
<summary>macOS: launchd (starts at login)</summary>

Save as `~/Library/LaunchAgents/dev.review-inbox.plist`, fix the paths, then run `launchctl load ~/Library/LaunchAgents/dev.review-inbox.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.review-inbox</string>
  <key>WorkingDirectory</key><string>/path/to/sglang-review-inbox</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/env</string><string>python3</string><string>server.py</string>
    <string>--config</string><string>config.json</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/review-inbox.log</string>
  <key>StandardErrorPath</key><string>/tmp/review-inbox.log</string>
</dict></plist>
```
</details>

<details>
<summary>Linux: systemd user service</summary>

```ini
# ~/.config/systemd/user/review-inbox.service
[Unit]
Description=SGLang Review Inbox

[Service]
WorkingDirectory=%h/sglang-review-inbox
ExecStart=/usr/bin/env python3 server.py --config config.json
Restart=on-failure

[Install]
WantedBy=default.target
```

`systemctl --user enable --now review-inbox`
</details>

**Restarting safely.** A restart drops agent jobs that are still running. They are retried later, except that an interrupted fix must not stop between push and comment. Before restarting, wait until `curl -s http://127.0.0.1:8765/api/state` shows `"inflight": {}`.

## Configuration reference

Everything has a default in [`inbox/config.py`](inbox/config.py); `config.json` only overrides what you set.

| Key | Meaning |
| --- | --- |
| `mode` | `shadow` (nothing written) or `live`. Also toggled from the dashboard. |
| `repos` | Repositories the agent may act on. Anything else is ignored. |
| `data_dir` | State: SQLite database, Slack tokens, page token, clones, run logs. Default `~/.cache/review-inbox`. |
| `sources.*` | Where asks come from and how often they are polled: `slack`, `slack_inbox` (DMs), `github_mentions`, `github_assigned`, `github_review_requested` (off by default; CODEOWNERS makes it huge). |
| `agent.codex_homes` | Codex accounts in order of preference (see above). |
| `agent.profiles` | Model and reasoning effort per job. |
| `agent.service_tier` | Passed to codex as `service_tier` (e.g. `priority` for faster, more expensive runs). |
| `agent.max_concurrent`, `agent.daily_review_limit` | Parallel agent jobs and reviews per 24 h. |
| `agent.review_skill` | Git repo, branch and folder of the review skill, synced every 10 minutes. A review that skips the skill is rejected. |
| `policy.auto_*` | One switch per kind of autonomous write: CI commands, posting findings, author notes, answering replies, approving, merging, pushing fixes. |
| `policy.max_*_per_day`, `max_agent_fixes_per_pr` | Caps. The merge cap counts only the agent's own merges; running fixes count against the fix cap. |
| `policy.min_merge_interval_minutes`, `policy.min_fix_interval_minutes` | Spacing: auto-merges land at least this far apart (default 60), and fixes run one at a time with pushes at least this far apart (default 15). A PR waiting for its slot stays with the agent. |
| `policy.slack_*` | Whether and how to answer Slack threads; replies start with `slack_prefix`. |
| `policy.github_prefix` | Starts every review, comment and reply the agent posts on GitHub (CI slash commands stay bare). Inline comments carry no `[P1]`/`[P2]` tags: a must-fix bug is plain, smaller points start with `Minor:`, `nit:` or `Non-blocking:`. |
| `refresh_seconds` | How often active PRs and PRs waiting on the author are re-checked. |
| `server.host`, `server.port` | Defaults `127.0.0.1:8765`. Keep it on localhost. |

## Using it outside SGLang

The flow is generic, but some parts encode SGLang's conventions: CI lanes and slash commands (`inbox/ci.py`), risk areas by path (`inbox/triage.py`), and the review prompts and skill. To point it at another project, start there.

## Project layout

```
server.py            HTTP server and JSON API (stdlib only)
inbox/pipeline.py    the per-PR state machine: intake, review, CI, discussion, fixes, merge, Slack replies
inbox/agent.py       codex runs: review, verify, CI triage, discuss, fix, translate; account fallback
inbox/ci.py          CI policy for SGLang (lanes, retries, merge bar)
inbox/github.py      gh wrappers;  inbox/slack.py  Slack Web API;  inbox/db.py  SQLite store and ledger
inbox/fixer.py       git for agent fixes (merge, commit, fast-forward push)
prompts/             agent prompts;  web/index.html  the dashboard (single file)
tests/               unit tests (python3 -m unittest discover -s tests)
```

## License

[Apache-2.0](LICENSE)
