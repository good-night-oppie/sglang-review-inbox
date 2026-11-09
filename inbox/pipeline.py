"""Orchestrator: ingest @-mentions, advance each PR card through
triage → agent review → verification → CI → merge (auto-merge when policy allows).

Only this module writes to GitHub/Slack, and every write is claimed in the
action ledger keyed by (PR, head, event) so it happens at most once per head."""

import json
import re
import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import agent, ci, fixer, github
from .config import is_live, slack_tokens
from .db import ACTIVE_STAGES, pr_key
from .skill import SkillSync, SkillSyncError
from .slack import Slack, SlackError, dm_status, is_human, mention_record, mention_status
from .triage import priority, triage

HUNK = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
NO_LOG = "(not fetched: over the 20-log cap)"

# GitHub text is written the way the maintainer types it. Severity tags come
# from the finding's severity, not from the model, and em dashes are an AI tell.
SEVERITY_TAG = re.compile(r"^\s*(?:\[P[0-3]\]|(?:nit|minor|non-blocking)\s*:)\s*", re.I)
EM_DASH = re.compile(r"\s*—\s*")


def plain(text):
    """Agent-written text as posted: no leading severity tag, no em dashes."""
    text = (text or "").strip()
    while SEVERITY_TAG.match(text):
        text = SEVERITY_TAG.sub("", text, count=1)
    text = EM_DASH.sub(", ", text)
    return text[:1].upper() + text[1:]


def finding_comment(finding):
    """A finding's inline text, with the one level marker a maintainer would use:
    nothing for a must-fix bug, `Minor:`, `nit:`, or `Non-blocking:` for an ask
    about evidence."""
    if not agent.is_risk(finding):
        tag = "Non-blocking: "
    else:
        tag = {"minor": "Minor: ", "nit": "nit: "}.get(finding["severity"], "")
    return tag + plain(finding["comment"])


def ask_list(asks):
    return asks[0] if len(asks) == 1 else ", ".join(asks[:-1]) + " and " + asks[-1]


def evidence_line(failure):
    """The first line of triage evidence worth showing an author; empty when
    the agent had no log to read (its placeholders must never be quoted)."""
    for line in (failure.get("evidence") or "").strip().splitlines():
        line = line.strip().strip("`\"'").strip()
        if line and not line.startswith("(") and not github.log_missing(line):
            return line
    return ""

# Slack thread replies: one short English line, sent only for the outcomes
# listed in policy["slack_outcomes"].
SLACK_TEXT = {
    "approved": "Approved {pr}, will merge once CI is green.",
    "changes": "{pr}: {n} thing(s) to fix, see the PR comments: {url}",
    "bounce": "{pr}: before review, please {reasons}.",
    "ci_related": "{pr}: CI failures look related to the change: {names}",
    "merged": "Merged {pr}, thanks!",
}


def new_side_ranges(diff_text):
    """Lines GitHub accepts for inline comments: new side of each hunk."""
    ranges, path = {}, None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("@@") and path:
            match = HUNK.match(line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                if count:
                    ranges.setdefault(path, []).append([start, start + count - 1])
    return ranges


def anchored(finding, ranges):
    return any(lo <= finding.get("line", 0) <= hi for lo, hi in ranges.get(finding.get("path"), []))


def epoch(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return 0.0


class Pipeline:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.policy = config["policy"]
        self.repos = set(config["repos"])
        self.login = config["github_login"]
        self.executor = ThreadPoolExecutor(max_workers=config["agent"]["max_concurrent"])
        self.inflight = {}
        # Chinese translations run one at a time on their own thread, so they
        # never take a review slot.
        self.translator = ThreadPoolExecutor(max_workers=1)
        self.translating = None
        self.translate_after = {}
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.status = {"started_at": time.time(), "last_tick": 0, "last_error": "", "sources": {}}
        self.refresh_all = True  # re-check every active card once after a restart
        self._slack = []
        self._pr_states = {}  # PR state cache for Slack mentions without a card
        self.skills = SkillSync(config) if config["agent"]["review_skill"].get("repo") else None
        self.config["mode"] = store.get_kv("mode", config["mode"])
        self.translate_wanted = set()  # cards someone opened in the Chinese view
        agent.usage_hook = self.record_usage

    # ---- loop -------------------------------------------------------------
    def start(self):
        threading.Thread(target=self.loop, name="pipeline", daemon=True).start()

    def loop(self):
        while not self.stopping.is_set():
            try:
                self.tick()
                self.status["last_error"] = ""
            except Exception:
                self.status["last_error"] = traceback.format_exc()[-1500:]
            self.status["last_tick"] = time.time()
            self.wake.wait(20)
            self.wake.clear()

    def tick(self):
        self.sync_skill()
        self.poll_sources()
        refresh_all, self.refresh_all = self.refresh_all, False
        for card in self.store.list_prs(ACTIVE_STAGES):
            if card["key"] in self.inflight or not (refresh_all or self.refresh_due(card)):
                continue
            try:
                self.advance(card)
            except Exception as exc:  # one bad card must not stall the others
                self.store.add_event(card["key"], "error", f"refresh failed: {str(exc)[:500]}")
                self.store.update_pr(card["key"], refreshed_at=time.time())
        self.announce_recent_merges()
        self.queue_translation()

    def sync_skill(self):
        """Keep the skill clone current even when no review runs: your own
        Codex/Claude skill links point at it too."""
        if not self.skills:
            return
        try:
            _, commit = self.skills.ensure()
            self.status["skill"] = {"ok": True, "commit": commit[:12]}
        except SkillSyncError as exc:
            self.status["skill"] = {"ok": False, "note": str(exc)[:300]}

    def refresh_due(self, card):
        if card["stage"] == "new" or card["decision"].get("refresh"):
            return True
        if card["stage"] == "error":  # retried forever: 5 min, doubling, at most hourly
            interval = min(300 * 2 ** max(card["decision"].get("error_count", 1) - 1, 0), 3600)
            return time.time() - (card["refreshed_at"] or 0) >= interval
        interval = self.config["refresh_seconds"][
            "waiting_author" if card["stage"] == "needs_author" else "active"]
        return time.time() - (card["refreshed_at"] or 0) >= interval

    # ---- Chinese view ------------------------------------------------------
    @staticmethod
    def agent_texts(card):
        """English text the agent wrote for this card (not code, logs, PR titles,
        people's messages or the GitHub comment drafts)."""
        review, texts = card["review"] or {}, []
        texts += [review.get(name) for name in ("summary", "verdict_reason", "corpus_synthesis", "coverage_notes",
                                                "mermaid")]
        texts += list(review.get("test_gaps") or [])
        for finding in (review.get("findings") or []) + (review.get("dropped") or []):
            texts += [finding.get(name) for name in ("title", "scenario", "fix", "verify_reason")]
        texts.append(((card["ci"] or {}).get("triage") or {}).get("summary"))
        return [text for text in dict.fromkeys(texts) if isinstance(text, str) and text.strip()]

    def missing_translations(self, key):
        card = self.store.get_pr(key)
        if card is None:
            return []
        texts = self.agent_texts(card)
        known = self.store.translations(texts)
        return [text for text in texts if text not in known]

    def request_translation(self, key):
        """On demand: a card opened in the Chinese view. True while its text is
        still being translated."""
        if not self.config["agent"].get("translate_zh", True) or not self.missing_translations(key):
            return False
        self.translate_wanted.add(key)
        self.wake.set()
        return True

    def queue_translation(self):
        """Translate the cards someone opened in the Chinese view, one at a
        time; the token budget applies, the agent windows do not."""
        if self.translating or not self.translate_wanted or self.agent_gate(window=False):
            return
        now = time.time()
        for key in list(self.translate_wanted):
            missing = self.missing_translations(key)
            if not missing:
                self.translate_wanted.discard(key)
                continue
            if self.translate_after.get(key, 0) > now:
                continue
            self.translating = key
            self.translator.submit(self.job_translate, key, missing)
            return

    def job_translate(self, key, texts):
        try:
            for i in range(0, len(texts), 40):
                self.store.put_translations(agent.translate_zh(self.config, texts[i:i + 40]))
            self.translate_after[key] = time.time() + 600  # anything the model skipped: later
        except Exception as exc:
            self.translate_after[key] = time.time() + 1800
            self.status["translate_error"] = f"{key}: {self.agent_blocked(exc) or str(exc)[:300]}"
        finally:
            self.translating = None
            self.wake.set()

    def kick(self, reset_sources=False):
        if reset_sources:
            for source in self.status["sources"].values():
                source["at"] = 0
            self.refresh_all = True
        self.wake.set()

    # ---- sources ----------------------------------------------------------
    def poll_sources(self):
        pollers = {"slack": self.poll_slack, "slack_inbox": self.poll_slack_inbox,
                   "github_mentions": self.poll_github_mentions, "github_assigned": self.poll_github_assigned,
                   "github_review_requested": self.poll_review_requested}
        for name, poller in pollers.items():
            source_config = self.config["sources"][name]
            state = self.status["sources"].setdefault(name, {"at": 0})
            if not source_config["enabled"]:
                state.update(ok=False, off=True, note="off")
                continue
            state["off"] = False
            if time.time() - state["at"] < source_config["poll_seconds"]:
                continue
            state["at"] = time.time()
            try:
                found = poller(source_config)
                state.update(ok=True, note=f"{found} new", checked=time.time())
            except Exception as exc:
                state.update(ok=False, note=str(exc)[:300], checked=time.time())

    def slack_clients(self):
        tokens = slack_tokens(self.config)
        if not tokens:
            slack = self.config["sources"]["slack"]
            raise RuntimeError(f"no Slack token ({slack['token_file']} or ${slack['token_env']})")
        if [client.token for client in self._slack] != tokens:
            self._slack = [Slack(token) for token in tokens]
        return self._slack

    def slack_teams(self):
        """Workspace ids of the tokens connected right now."""
        try:
            return {client.identity() and client.team_id for client in self.slack_clients()}
        except Exception:
            return set()

    def slack_for(self, team_id):
        for client in self.slack_clients():
            client.identity()
            if client.team_id == team_id:
                return client
        raise RuntimeError(f"no token for workspace {team_id}")

    def poll_slack(self, source_config):
        """Only @-mentions that carry a PR link and ask for review become cards;
        every other mention is listed under Slack and never answered."""
        intent = re.compile(source_config["review_intent"], re.I)
        fyi = re.compile(source_config["fyi_pattern"], re.I)
        fresh_after = time.time() - source_config["review_max_age_days"] * 86400
        added = 0
        for client in self.slack_clients():
            uid = client.identity()
            for query in source_config["queries"]:
                for match in client.search(query.replace("{uid}", uid), source_config["lookback_days"]):
                    record = mention_record(match, client.team_id)
                    if match.get("user") == uid or self.store.has_mention(record["id"]):
                        continue
                    if match.get("bot_id") or not (record["text"] or "").strip():
                        continue  # integrations and empty bot posts are not requests
                    own = record["text"]
                    refs = github.find_pr_refs(own, self.repos)
                    if not refs and source_config["follow_threads"] and record["thread_ts"] != match.get("ts"):
                        try:
                            parent = client.thread_parent_text(record["channel_id"], record["thread_ts"])
                        except Exception:
                            parent = ""
                        refs = github.find_pr_refs(parent, self.repos)
                    asks = bool(intent.search(own))
                    readable = client.render(own)
                    if refs and asks and record["ts"] >= fresh_after:
                        record.update(kind="review", status="agent")
                    elif refs and asks:
                        record.update(kind="review-old", status="pending")  # too old to act on unasked
                    elif fyi.search(re.sub(r"<@[^>]+>", "", own).strip()):
                        record.update(kind="fyi", status="pending")
                    else:
                        record.update(kind="pr-other" if refs else "other", status="pending")
                    record["pr_keys"] = [pr_key(repo, number) for repo, number in refs] if refs else []
                    record["text"] = readable
                    self.store.add_mention(record)
                    added += 1
                    if record["kind"] == "review":
                        self.register_slack_request(client, record, refs)
            for channel in source_config.get("watch_channels") or []:
                added += self.poll_watched_channel(client, uid, channel, source_config, fyi)
        return added

    def poll_watched_channel(self, client, uid, channel, source_config, fyi):
        """PRs posted in a team PR channel are yours to review even without an
        @-mention, unless the post names someone else or someone else already
        reviews the PR on GitHub. Posts that @-mention you go through the
        regular mention flow."""
        fresh_after = time.time() - source_config["watch_max_age_days"] * 86400
        added = 0
        for match in client.search(f"in:#{channel}", source_config["watch_max_age_days"]):
            record = mention_record(match, client.team_id)
            text = record["text"] or ""
            if self.store.has_mention(record["id"]) or match.get("user") == uid or not is_human(match):
                continue
            refs = github.find_pr_refs(text, self.repos)
            if not refs or record["ts"] < fresh_after or f"<@{uid}>" in text:
                continue
            if re.search(r"<@\w+|<!subteam\^", text) or fyi.search(text):
                refs = []  # addressed to someone else
            else:
                refs = [ref for ref in refs if not self.reviewed_by_others(*ref)]
            record.update(kind="review" if refs else "channel-other", status="agent" if refs else "ignored",
                          pr_keys=[pr_key(*ref) for ref in refs], text=client.render(text))
            self.store.add_mention(record)
            added += 1
            if refs:
                self.register_slack_request(client, record, refs)
        return added

    def reviewed_by_others(self, repo, number):
        """Someone other than you, the author and bots already reviewed it."""
        try:
            reviews = github.gh_json(["api", "-X", "GET", f"repos/{repo}/pulls/{number}/reviews",
                                      "-f", "per_page=100"]) or []
            author = (github.gh_json(["api", f"repos/{repo}/pulls/{number}"]) or {}).get("user", {}).get("login")
        except github.GitHubError:
            return False
        return any(r.get("user", {}).get("login") not in (self.login, author)
                   and not r.get("user", {}).get("login", "").endswith("[bot]") for r in reviews)

    def register_slack_request(self, client, record, refs):
        # DM ingress used to attach only a requester. Persist explicit human
        # asks as source evidence too; link-only automatic asks get no override.
        intent = re.compile(self.config["sources"]["slack"]["review_intent"], re.I)
        if (re.fullmatch(r"[UW][A-Z0-9]+", record.get("user_id", ""))
                and intent.search(record.get("text", "")) and not self.store.has_mention(record["id"])):
            self.store.add_mention(dict(record, source="slack", team_id=client.team_id,
                conv=record.get("conv") or ("dm" if record.get("channel") == "DM" else "channel"),
                kind="review", status="agent", pr_keys=[pr_key(*ref) for ref in refs]))
        for repo, number in refs:
            self.store.ensure_pr(repo, number, requester={
                "id": record["id"], "source": "slack", "author": record["author"],
                "user_id": record.get("user_id", ""),
                "team_id": client.team_id, "channel": record["channel"],
                "channel_id": record["channel_id"], "thread_ts": record["thread_ts"],
                "permalink": record["permalink"], "text": record["text"][:300], "ts": record["ts"]})

    def poll_slack_inbox(self, source_config):
        """What you still owe a reply: @-mentions you have not answered or
        reacted to, and DMs where the other side spoke last. Review requests
        found in DMs go to the pipeline like @-mentions do."""
        since = time.time() - source_config["lookback_days"] * 86400
        intent = re.compile(self.config["sources"]["slack"]["review_intent"], re.I)
        changed = 0
        for client in self.slack_clients():
            me = client.identity()
            for item in self.store.recent_mentions(300, statuses=("pending",), since=since):
                if item["source"] != "slack" or item["team_id"] != client.team_id or item["id"].startswith("slackdm:"):
                    continue
                status = self.mention_handled(client, me, item)
                if status != "pending":
                    self.store.set_mention_status(item["id"], status)
                    changed += 1
            for conv in self.recent_dms(client, source_config):
                try:
                    messages = client.history(conv["id"], since, limit=30)
                    messages = self.with_thread_replies(client, conv, messages, since)
                except SlackError:
                    continue
                item_id = f"slackdm:{client.team_id}:{conv['id']}"
                state = dm_status(me, messages, group=not conv.get("is_im"))
                if state is None:
                    self.dm_review_asks(client, me, conv, messages, intent)
                    if self.store.has_mention(item_id):
                        self.store.upsert_dm({"id": item_id, "ts": 0, "status": "replied", "text": "",
                                              "permalink": "", "author": ""})
                    continue
                asked = self.dm_review_asks(client, me, conv, messages, intent)
                status, latest, count = state
                if not conv.get("is_im") and self.store.has_mention(
                        f"slack:{client.team_id}:{conv['id']}:{latest['ts']}"):
                    continue  # a group-DM @ already listed under mentions
                author = client.user_name(latest.get("user", ""))
                raw = latest.get("text", "")
                refs = github.find_pr_refs(raw, self.repos)
                if latest["ts"] in asked:
                    status = "agent"
                self.store.upsert_dm({
                    "id": item_id, "source": "slack", "kind": "dm", "conv": "dm", "team_id": client.team_id,
                    "channel_id": conv["id"], "channel": "DM" if conv.get("is_im") else (conv.get("name") or "group DM"),
                    "author": author, "text": client.render(raw), "ts": float(latest["ts"]),
                    "permalink": client.permalink(conv["id"], latest["ts"]), "status": status, "count": count,
                    "pr_keys": [pr_key(*ref) for ref in refs] if status == "agent" else []})
                changed += 1
        self.resolve_closed_pr_mentions(since)
        for client in self.slack_clients():
            changed += self.follow_mention_threads(client, client.identity(), intent)
            changed += self.promote_missed_asks(client, intent)
        return changed

    def promote_missed_asks(self, client, intent):
        """Mentions stored as "not an ask" or "too old" that the current rules
        call a review ask, and whose PRs are still open, go to the agent."""
        slack = self.config["sources"]["slack"]
        fresh_after = time.time() - slack["review_max_age_days"] * 86400
        fyi = re.compile(slack["fyi_pattern"], re.I)
        promoted = 0
        for item in self.store.recent_mentions(300, statuses=("pending",), since=fresh_after):
            if item["team_id"] != client.team_id or item["kind"] not in ("pr-other", "review-old"):
                continue
            text = item["text"] or ""
            if not item["pr_keys"] or not intent.search(text) or fyi.search(re.sub(r"@\S+", "", text).strip()):
                continue
            if any(self.pr_state(key) != "OPEN" for key in item["pr_keys"]):
                continue
            refs = [tuple(key.split("#")) for key in item["pr_keys"]]
            self.store.set_mention_status(item["id"], "agent", "review ask (re-read with the current rules)")
            self.store.mark_review_ask(item["id"])
            self.register_slack_request(client, dict(item, thread_ts=item["thread_ts"] or f"{item['ts']:.6f}"),
                                        [(repo, int(number)) for repo, number in refs])
            promoted += 1
        return promoted

    def follow_mention_threads(self, client, me, intent):
        """A later reply in a thread that @-mentioned you, carrying a PR link
        and asking for a review without naming someone else, is an ask to you
        too; the outcome goes back into that thread."""
        slack = self.config["sources"]["slack"]
        fresh_after = time.time() - slack["review_max_age_days"] * 86400
        threads = {}
        for item in self.store.recent_mentions(300, since=fresh_after):
            if item["source"] == "slack" and item["team_id"] == client.team_id and item["thread_ts"] \
                    and not item["id"].startswith("slackdm:") and item["conv"] == "channel":
                threads.setdefault((item["channel_id"], item["thread_ts"]), item)
        added = 0
        for (channel_id, thread_ts), item in threads.items():
            try:
                replies = client.replies(channel_id, thread_ts)
            except SlackError:
                continue
            for message in replies:
                raw, ts = message.get("text", ""), float(message.get("ts", 0))
                record_id = f"slack:{client.team_id}:{channel_id}:{message.get('ts')}"
                if ts <= item["ts"] or ts < fresh_after or message.get("user") == me or not is_human(message):
                    continue
                if self.store.has_mention(record_id) or re.search(r"<@\w+|<!subteam\^", raw):
                    continue
                refs = github.find_pr_refs(raw, self.repos)
                if not refs or not intent.search(raw):
                    continue
                record = {"id": record_id, "source": "slack", "team_id": client.team_id, "conv": "channel",
                          "user_id": message.get("user", ""), "channel": item["channel"], "channel_id": channel_id,
                          "author": client.user_name(message.get("user", "")), "text": client.render(raw),
                          "permalink": client.permalink(channel_id, message["ts"], thread_ts), "ts": ts,
                          "thread_ts": thread_ts, "kind": "review", "status": "agent",
                          "pr_keys": [pr_key(*ref) for ref in refs]}
                self.store.add_mention(record)
                self.register_slack_request(client, record, refs)
                added += 1
        return added

    def recent_dms(self, client, source_config):
        """DMs with recent messages to you (Slack search `to:me`, which also
        finds conversations far down Slack's priority order) plus the few
        Slack ranks highest."""
        found = {}
        for match in client.search("to:me", source_config["lookback_days"]):
            channel = match.get("channel") or {}
            if (channel.get("is_im") or channel.get("is_mpim")) and channel.get("id"):
                found.setdefault(channel["id"], {"id": channel["id"], "is_im": bool(channel.get("is_im")),
                                                 "is_mpim": bool(channel.get("is_mpim")), "name": channel.get("name")})
        for conv in client.dm_conversations(source_config["dm_conversations"]):
            found.setdefault(conv["id"], conv)
        return list(found.values())

    @staticmethod
    def with_thread_replies(client, conv, messages, since):
        """Top-level DM messages plus replies in their recent threads, newest
        first, so a question asked inside a DM thread is not missed."""
        replies = []
        for message in messages:
            if message.get("reply_count") and float(message.get("latest_reply") or 0) >= since:
                replies += [r for r in client.replies(conv["id"], message["ts"]) if r.get("ts") != message["ts"]]
        return sorted(messages + replies, key=lambda m: float(m.get("ts", 0)), reverse=True)

    def dm_review_asks(self, client, me, conv, messages, intent):
        """Every fresh DM message from someone else that carries a PR link is a
        review ask (in a group DM it must ask you or @ you); each gets its own
        request, so the outcome reply lands under that message. Returns the
        ts of the messages handed to the agent."""
        slack = self.config["sources"]["slack"]
        fresh_after = time.time() - slack["review_max_age_days"] * 86400
        fyi = re.compile(slack["fyi_pattern"], re.I)
        asked = set()
        for message in messages:
            raw = message.get("text", "")
            if message.get("user") == me or not is_human(message) or float(message.get("ts", 0)) < fresh_after:
                continue
            refs = github.find_pr_refs(raw, self.repos)
            if not refs or fyi.search(re.sub(r"<@[^>]+>", "", raw).strip()):
                continue
            if not conv.get("is_im") and not (intent.search(raw) or f"<@{me}>" in raw):
                continue
            self.register_slack_request(client, {
                "id": f"slack:{client.team_id}:{conv['id']}:{message['ts']}",
                "author": client.user_name(message.get("user", "")), "user_id": message.get("user", ""),
                "channel": "DM", "channel_id": conv["id"], "thread_ts": message.get("thread_ts") or message["ts"],
                "permalink": client.permalink(conv["id"], message["ts"]),
                "text": client.render(raw), "ts": float(message["ts"])}, refs)
            asked.add(message["ts"])
        return asked

    def resolve_closed_pr_mentions(self, since, budget=30):
        """A pending mention whose PRs are all merged or closed is no longer owed."""
        for item in self.store.recent_mentions(300, statuses=("pending",), since=since):
            if item["source"] != "slack" or not item["pr_keys"]:
                continue
            states = [self.pr_state(key) for key in item["pr_keys"]]
            budget -= sum(state is None for state in states)
            if states and all(state in ("MERGED", "CLOSED") for state in states):
                self.store.set_mention_status(item["id"], "resolved", "PR merged or closed")
            if budget <= 0:
                break

    def pr_state(self, key):
        card = self.store.get_pr(key)
        if card and card["refreshed_at"]:
            return card["state"]
        cached = self._pr_states.get(key)
        if cached and time.time() - cached[1] < 3600:
            return cached[0]
        repo, number = key.split("#")
        try:
            state = github.gh_json(["pr", "view", number, "--repo", repo, "--json", "state"])["state"]
        except (github.GitHubError, TypeError, KeyError):
            return None
        self._pr_states[key] = (state, time.time())
        return state

    def mention_handled(self, client, me, item):
        ts = f"{item['ts']:.6f}"
        thread = item["thread_ts"] or ts
        try:
            messages = client.replies(item["channel_id"], thread)
            if thread == ts:  # top-level mention: an answer may also be a plain channel message
                messages += [m for m in client.history(item["channel_id"], item["ts"], limit=20)
                             if float(m.get("ts", 0)) - item["ts"] < 6 * 3600]
        except SlackError:
            return "pending"
        return mention_status(item["ts"], me, messages)

    ONCE_PER_PR = ("approved", "merged")  # a new head (e.g. an agent fix) is no news to the asker

    def slack_reply(self, key, head, outcome, **values):
        """Tell every Slack requester of this PR the current outcome, once per
        (requester, head, outcome), and "approved" / "merged" only once per PR;
        safe to call on every refresh."""
        if not (is_live(self.config) and self.policy["slack_report"]):
            return
        if outcome not in self.policy["slack_outcomes"]:
            return
        card = self.store.get_pr(key)
        pr = f"{card['repo'].split('/')[-1]}#{card['number']}"
        teams = self.slack_teams()
        for requester in card["requesters"]:
            if requester.get("source") != "slack" or not requester.get("thread_ts"):
                continue
            if requester.get("team_id") not in teams:
                continue  # that workspace is no longer connected
            event, claim_head, link = f"slack:{outcome}", head, f"<{card['url']}|{pr}>"
            thread = f"slackthread:{requester.get('team_id')}:{requester['channel_id']}:{requester['thread_ts']}"
            done = [a for owner in (requester["id"], thread) for a in self.store.actions(owner)
                    if a["event"] == event and a["phase"] == "done"]
            if outcome in self.ONCE_PER_PR and done:
                continue
            if any(a["head"] == head for a in done):
                continue  # another ask in this thread already got this outcome
            if outcome == "merged":
                # one closing line per ask, once every PR it named is closed
                link, claim_head = self.merged_in_ask(requester, key), "-"
                if not link:
                    continue
            if not self.store.claim_action(thread, claim_head, event):
                continue
            text = SLACK_TEXT[outcome].format(pr=link, login=self.login, **values)
            # No @: a reply in the requester's own thread already notifies them.
            text = self.policy["slack_prefix"].format(login=self.login) + text
            try:
                self.slack_for(requester["team_id"]).reply(requester["channel_id"], requester["thread_ts"], text)
            except Exception as exc:
                self.store.finish_action(thread, claim_head, event, False)
                self.store.add_event(key, "error", f"Slack reply failed: {exc}")
                continue
            self.store.finish_action(thread, claim_head, event, True)
            self.store.add_event(key, "action", f"Slack reply ({outcome}): {text}",
                                 {"url": requester.get("permalink")})

    def merged_in_ask(self, requester, key):
        """Slack links of the merged PRs one ask named, or "" while any of
        them is still open (or none got merged)."""
        mention = self.store.get_mention(requester["id"])
        cards = [self.store.get_pr(k) for k in (mention or {}).get("pr_keys") or [key]]
        if any(c is None or c["state"] == "OPEN" for c in cards):
            return ""
        links = [f"<{c['url']}|{c['repo'].split('/')[-1]}#{c['number']}>" for c in cards if c["state"] == "MERGED"]
        return " and ".join([", ".join(links[:-1]), links[-1]] if len(links) > 1 else links)

    def announce_recent_merges(self, days=3):
        """Slack asks whose PRs you merged yourself (by hand or through the
        agent) get their closing line, also when the merge happened outside
        this pipeline."""
        if not (is_live(self.config) and self.policy["slack_report"]):
            return
        for card in self.store.list_prs(("closed",)):
            if card["stage_reason"] != "merged" or time.time() - (card["updated_at"] or 0) > days * 86400:
                continue
            if not any(r.get("source") == "slack" for r in card["requesters"]):
                continue
            merged_by = (card["meta"] or {}).get("merged_by")
            if merged_by is None:
                try:
                    merged_by = ((github.gh_json(["pr", "view", str(card["number"]), "--repo", card["repo"],
                                                  "--json", "mergedBy"]) or {}).get("mergedBy") or {}).get("login", "")
                except github.GitHubError:
                    continue
                self.store.update_pr(card["key"], meta=dict(card["meta"] or {}, merged_by=merged_by))
            if merged_by == self.login:
                self.slack_reply(card["key"], card["head_sha"], "merged")

    def _github_search(self, qualifier, source, source_config):
        since = (datetime.now(timezone.utc) - timedelta(days=source_config["lookback_days"])).strftime("%Y-%m-%d")
        added = 0
        for repo in self.repos:
            items = github.search_issues(
                f"repo:{repo} is:pr is:open {qualifier}:{self.login} updated:>={since}"
                + ("" if self.config.get("review_own_prs") else f" -author:{self.login}"))
            for item in items:
                card, created = self.store.ensure_pr(repo, item["number"], requester={
                    "id": f"{source}:{repo}#{item['number']}", "source": source,
                    "author": (item.get("user") or {}).get("login", ""), "permalink": item.get("html_url", ""),
                    "text": item.get("title", ""), "ts": time.time()})
                added += created
                if not created and card and epoch(item.get("updated_at", "")) > (card["refreshed_at"] or 0):
                    self.store.update_pr(card["key"], decision=dict(card["decision"], refresh=True))
        return added

    def poll_github_mentions(self, source_config):
        return self._github_search("mentions", "github-mention", source_config)

    def poll_github_assigned(self, source_config):
        return self._github_search("assignee", "github-assigned", source_config)

    def poll_review_requested(self, source_config):
        added = self._github_search("review-requested", "github-review-request", source_config)
        return added + self.reconcile_open_prs()

    def reconcile_open_prs(self):
        """Backfill cards whose review request was cleared between search polls.

        Existing cards and drafts are left alone. List/timeline errors propagate to
        poll_sources so an incomplete reconciliation is never reported as healthy.
        """
        added = 0
        for repo in sorted(self.repos):
            for item in github.list_open_prs(repo):
                if item.get("isDraft"):
                    continue
                key = pr_key(repo, item["number"])
                if self.store.get_pr(key) is not None:
                    continue
                lines = (item.get("body") or "").splitlines()
                wanted = bool(lines) and lines[0] == "QQ 请审"
                if not wanted:
                    events = github.pr_timeline(repo, item["number"])
                    wanted = any(event.get("event") == "review_requested"
                                 and (event.get("requested_reviewer") or {}).get("login") == self.login
                                 for event in events)
                if not wanted:
                    continue
                _, created = self.store.ensure_pr(repo, item["number"], requester={
                    "id": f"reconcile:{repo}#{item['number']}", "source": "reconcile",
                    "author": (item.get("author") or {}).get("login", ""),
                    "permalink": item.get("url", ""), "text": item.get("title", ""), "ts": time.time()})
                added += created
        return added

    # ---- per-PR state machine -------------------------------------------
    def advance(self, card):
        key = card["key"]
        snap = github.fetch_pr(card["repo"], card["number"])
        head = snap["headRefOid"]
        mine = [r for r in snap.get("latestReviews") or [] if (r.get("author") or {}).get("login") == self.login]
        if card["head_sha"] and card["head_sha"] != head:
            self.store.add_event(key, "push", f"new push: {card['head_sha'][:8]} → {head[:8]}")
        decision = dict(card["decision"], refresh=False)
        self.store.update_pr(
            key, title=snap["title"], author=(snap.get("author") or {}).get("login", ""),
            url=snap["url"], state=snap["state"], draft=snap["isDraft"], head_sha=head,
            base_ref=snap["baseRefName"], additions=snap["additions"], deletions=snap["deletions"],
            changed_files=snap["changedFiles"], labels=[l["name"] for l in snap.get("labels") or []],
            mergeable=snap.get("mergeable") or "", review_decision=snap.get("reviewDecision") or "",
            meta={"merge_state": snap.get("mergeStateStatus"), "created_at": snap.get("createdAt"),
                  "updated_at": snap.get("updatedAt"),
                  "my_review": mine[0].get("state") if mine else ""},
            decision=decision, refreshed_at=time.time())
        card = self.store.get_pr(key)

        if snap["state"] != "OPEN":
            merged_by = (snap.get("mergedBy") or {}).get("login", "")
            self.store.update_pr(key, meta=dict(self.store.get_pr(key)["meta"] or {}, merged_by=merged_by))
            self.store.set_stage(key, "closed", "merged" if snap["state"] == "MERGED" else "closed")
            if snap["state"] == "MERGED" and merged_by == self.login:  # yours, by hand or by the agent
                self.slack_reply(key, head, "merged")
            return
        # jj-agent overlay: every teammate opens PRs from the one shared account the
        # Inbox runs as, so `review_own_prs` keeps them instead of dismissing them.
        if card["author"] == self.login and not self.config.get("review_own_prs"):
            self.store.set_stage(key, "dismissed", "your own PR")
            return

        result = triage(snap, self.policy, requested_by_human=bool(card["requesters"]))
        self.store.update_pr(key, triage=result, tier=result["tier"], priority=priority(card, result))
        # Every PR is followed until it is merged: replies are read and answered
        # at any stage, against the current review (a new push is reviewed first).
        if (not card["review"] or card["review_head"] == head) and self.answer_replies(card, snap):
            return
        # Someone asking you in Slack (or you adding it) wants a review even of a draft.
        asked = any(r.get("source") in ("slack", "manual") for r in card["requesters"])
        gates = [g for g in result["gates"] if g["kind"] == "author" and not (g["id"] == "draft" and asked)]
        if gates and decision.get("force_review_head") != head:
            self.store.set_stage(key, "needs_author", "; ".join(g["reason"] for g in gates))
            told = [g for g in gates if g["tell_author"]]
            if told:
                # Keyed by which gates fired, so a conflict that appears after a
                # missing-tests note on the same head still gets its own reminder.
                self.author_note(key, head, "github:bounce:" + "+".join(sorted(g["id"] for g in told)),
                                 f"Could you {ask_list([g['ask'] for g in told])}?")
                self.slack_reply(key, head, "bounce", reasons="; ".join(g["ask"] for g in told))
            return

        if (card["review_head"] != head or not card["review"]) and self.carry_over(card, snap):
            card = self.store.get_pr(key)  # only main merged in: the review still stands
        if card["review_head"] != head or not card["review"]:
            wait = self.agent_gate(card)  # the window and the token cap first: they say when it runs
            if wait:
                self.store.set_stage(key, "queued", wait)
                return
            used = self.store.count_events("review", time.time() - 86400)
            if used >= self.config["agent"]["daily_review_limit"]:
                self.store.set_stage(key, "queued", f"daily agent-review budget used ({used}/24h), queued")
                return
            self.store.set_stage(key, "queued", f"waiting for agent review ({result['tier']})")
            self.submit(key, self.job_review, key, snap, result["tier"], self.delta_base(card))
            return

        review = card["review"]
        waived = set(card["decision"].get("waived_findings") or [])
        verdict = agent.effective_verdict({"verdict": review.get("agent_verdict", review["verdict"])},
                                          [f for f in review["findings"] if f["title"] not in waived])
        if verdict != review["verdict"]:
            review = dict(review, verdict=verdict)
            self.store.update_pr(key, review=review)
        # Approval is a current decision, not a permanent property of the SHA.
        # reviewDecision survives a push; only a latest CHANGES_REQUESTED bound to this head blocks it.
        if self.changes_requested_on_head(snap.get("reviews") or [], head):
            decision = dict(card["decision"])
            decision.pop("approved_head", None)
            decision["merge_ready"] = False
            self.store.update_pr(key, decision=decision)
            self.store.set_stage(key, "needs_author", "GitHub review requests changes on this head")
            return
        approved = self.approved_this_head(card, head)
        card = self.store.get_pr(key)
        decision = card["decision"]
        if decision.get("approve_head") == head and not approved:
            self.approve(key, note="", by="you, in a GitHub comment")  # you said LGTM on the PR
            # A model's interpretation of a comment is advisory, never a formal approval.
        if decision.get("needs_you_head") == head and not approved:
            self.set_ready(card, "look", decision.get("needs_you_reason") or "the author disputes a finding")
            return
        if decision.get("waiting_author_head") == head and not approved:
            # You sent the findings and handed it to the author: hold it until they push.
            self.store.set_stage(key, "needs_author", "you confirmed the findings and sent them to the author")
            return
        if review["verdict"] == "changes_needed" and not approved:
            confirmed = [f for f in review["findings"] if f["title"] not in waived and agent.is_risk(f)
                         and f["severity"] in ("blocker", "major") and f["verify"] == "confirmed"]
            issues = [{"id": f"F{i + 1}", "title": f["title"], "path": f["path"], "line": f["line"],
                       "scenario": f["scenario"], "evidence": f.get("evidence", ""), "suggested_fix": f.get("fix", "")}
                      for i, f in enumerate(confirmed)]
            if self.try_fix(card, snap, "findings", issues):
                return
            published = self.findings_review(key, head)
            if not published and confirmed and all(f.get("raised_before") for f in confirmed) \
                    and self.latest_findings_review(key):  # nothing new since the last posted review
                self.store.set_stage(key, "needs_author", "blocking issues confirmed, already posted, "
                                     "waiting for the author")
                return
            if not published and is_live(self.config) and self.policy["auto_publish_findings"]:
                published = self.publish_findings(key, by="agent")
            self.store.set_stage(key, "needs_author", "blocking issues confirmed" + (", posted on the PR, waiting for the author" if published else ""))
            if published:
                blocking = [f for f in review["findings"] if f["severity"] in ("blocker", "major")]
                self.slack_reply(key, head, "changes", n=len(blocking), url=published.get("url", card["url"]))
            return

        # Approval is about the code alone: it never waits for CI or a clean merge.
        if not approved:
            if review["verdict"] == "needs_human" or card["tier"] not in self.auto_approve_tiers():
                self.set_ready(card, "look", "needs your look: " + (review.get("verdict_reason") or "high-risk change"))
            elif self.auto_approve_allowed():
                self.approve(key, by="agent")
            elif is_live(self.config) and self.policy["auto_approve"]:
                self.store.set_stage(key, "queued", "no blocking issues; daily approval budget used, "
                                     "approving when it frees up")
                return
            else:
                self.set_ready(card, "approve", "no blocking issues, ready to approve")
        # CI runs either way so the evidence is ready; it only decides merge readiness.
        self.handle_ci(self.store.get_pr(key), snap, merge_gate=approved)

    # ---- answering replies on the agent's review comments --------------------
    def answer_replies(self, card, snap):
        if not (is_live(self.config) and self.policy["auto_discuss"]):
            return False
        if self.agent_gate(card):  # outside the agent's window replies wait for it
            return False
        replies = self.open_replies(card)
        if not replies:
            return False
        self.store.set_stage(card["key"], "reviewing", f"agent is answering {len(replies)} reply(ies) on the PR")
        self.submit(card["key"], self.job_discuss, card["key"], snap, replies)
        return True

    def findings_review_any(self, key):
        return any(a["event"] == "review:findings" and a["phase"] == "done" for a in self.store.actions(key))

    def agent_comment_ids(self, key):
        """GitHub comment ids the agent posted on this PR (from the ledger)."""
        ids = set()
        for action in self.store.actions(key):
            ids |= {int(x) for x in re.findall(r"(?:issuecomment-|discussion_r)(\d+)", json.dumps(action["result"] or {}))}
        return ids

    def open_replies(self, card):
        """What the PR wants an answer to: replies in review threads you
        started, and the author's PR comments once you have said something on
        the PR (or that @-mention you), newer than your last word there and
        not handled yet. Slash commands are not replies."""
        since = datetime.fromtimestamp(time.time() - 14 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            items = github.discussion(card["repo"], card["number"], since)
        except github.GitHubError:
            return []
        answered = {a["event"].split(":", 1)[1] for a in self.store.actions(card["key"]) if a["event"].startswith("reply:")}
        agent_ids = self.agent_comment_ids(card["key"])
        first_seen = datetime.fromtimestamp(card["first_seen"] or 0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for c in items:  # you typing on GitHub yourself, as opposed to the agent posting as you
            c["maintainer"] = (c["author"] == self.login and c["id"] not in agent_ids
                               and (c["kind"] == "issue" or c["in_reply_to"] is not None))
        mine = {c["id"] for c in items if c["author"] == self.login and c["kind"] == "review"}
        last_mine, mention = {}, f"@{self.login}".lower()
        for c in items:
            if c["author"] == self.login:
                last_mine[c["in_reply_to"] or c["id"] if c["kind"] == "review" else "issue"] = c["created_at"]
        found = []
        for c in items:
            if str(c["id"]) in answered:
                continue
            if c["maintainer"]:
                # your own word is a decision: follow it (never answered)
                if c["created_at"] >= first_seen and not c["body"].lstrip().startswith("/"):
                    found.append(c)
                continue
            if c["author"] in (self.login, "") or c["author"].endswith("[bot]"):
                continue
            if c["kind"] == "review" and c["in_reply_to"] in mine:
                if c["created_at"] > last_mine.get(c["in_reply_to"], ""):
                    found.append(c)
            elif c["kind"] == "issue" and c["author"] == card["author"] and not c["body"].lstrip().startswith("/"):
                talked = "issue" in last_mine or bool(mine)
                if (talked or mention in c["body"].lower()) and c["created_at"] > last_mine.get("issue", ""):
                    found.append(c)
        return found

    def job_discuss(self, key, snap, replies):
        card = self.store.get_pr(key)
        head = snap["headRefOid"]
        waived = set(card["decision"].get("waived_findings") or [])
        findings = [{"title": f["title"], "severity": f["severity"], "path": f["path"], "line": f["line"],
                     "scenario": f["scenario"], "comment": f["comment"]}
                    for f in (card["review"] or {}).get("findings") or [] if f["title"] not in waived]
        since = datetime.fromtimestamp(time.time() - 14 * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        items = github.discussion(card["repo"], card["number"], since)
        threads = []
        for reply in replies:
            if reply["kind"] == "review":
                thread = [c for c in items if reply["in_reply_to"] in (c["id"], c["in_reply_to"])
                          and c["created_at"] <= reply["created_at"]]
            else:
                thread = [c for c in items if c["kind"] == "issue" and c["created_at"] <= reply["created_at"]][-4:]
            threads.append({"comment_id": str(reply["id"]), "path": reply.get("path"), "line": reply.get("line"),
                            "from_maintainer": bool(reply.get("maintainer")),
                            "conversation": [{"author": c["author"], "body": c["body"][:3000]} for c in thread]})
        workspace = agent.Workspace(self.config, card["repo"], card["number"], head, snap["baseRefName"])
        try:
            workspace.prepare(snap)
            result, _ = agent.discuss(self.config, workspace, findings, threads)
        finally:
            workspace.cleanup()
        by_id = {str(r["id"]): r for r in replies}
        maintainer, followed = [], []
        ci_waived = set(card["decision"].get("ci_waived") or [])
        for answer in result.get("answers", []):
            reply = by_id.get(answer["comment_id"])
            if not reply:
                continue
            event = f"reply:{reply['id']}"
            if reply.get("maintainer"):  # your instruction: follow it, never answer it
                if self.store.claim_action(key, head, event):
                    self.store.finish_action(key, head, event, True, {"decision": answer["decision"], "by": "you"})
                    self.store.add_event(key, "decision", f"following your GitHub comment ({answer['decision']}): "
                                         + reply["body"].strip()[:160], {"url": reply["url"]})
                    followed.append(answer)
                continue
            if answer["decision"] == "none" or not answer["reply"].strip():
                if self.store.claim_action(key, head, event):  # a thanks or a status update: read, no answer
                    self.store.finish_action(key, head, event, True, {"decision": "none"})
                continue
            if not self.store.claim_action(key, head, event):
                continue
            body = plain(answer["reply"])
            try:
                if reply["kind"] == "review":
                    url = github.reply_in_thread(card["repo"], card["number"], reply["in_reply_to"],
                                                 github.agent_review_body(head, self.sign(body)))
                else:
                    url = github.comment(card["repo"], card["number"],
                                         github.agent_review_body(head, self.sign(f"@{reply['author']} {body}")))
            except github.GitHubError as exc:
                self.store.finish_action(key, head, event, False)
                self.store.add_event(key, "error", f"answering {reply['author']} failed: {exc}")
                continue
            self.store.finish_action(key, head, event, True, {"url": url, "decision": answer["decision"]})
            self.store.add_event(key, "action", f"agent answered {reply['author']} ({answer['decision']}): {body[:160]}",
                                 {"url": url})
            if answer["decision"] == "concede" and answer["finding"] and answer.get("about") == "ci":
                ci_waived.add(answer["finding"])
            elif answer["decision"] == "concede" and answer["finding"]:
                waived.add(answer["finding"])
            elif answer["decision"] == "maintainer":
                maintainer.append(answer["reply"])
        for answer in followed:  # what your own comments decided
            if answer["decision"] == "concede" and answer["finding"]:
                (ci_waived if answer.get("about") == "ci" else waived).add(answer["finding"])
        decision = dict(self.store.get_pr(key)["decision"], waived_findings=sorted(waived), ci_waived=sorted(ci_waived))
        if followed:  # you have spoken: it is no longer waiting on you
            decision.update(needs_you_head="", needs_you_reason="")
            if any(a["decision"] == "approve" for a in followed):
                decision["approve_head"] = head
            elif any(a["decision"] == "hold" for a in followed):
                decision["waiting_author_head"] = head
        if maintainer:
            decision.update(needs_you_head=head, needs_you_reason="the author disputes a finding: " + maintainer[0][:200])
        self.store.update_pr(key, decision=decision)

    @staticmethod
    def changes_requested_on_head(reviews, head):
        latest = {}
        for review in reviews:
            author = (review.get("author") or {}).get("login")
            if not author or review.get("state") not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                continue
            previous = latest.get(author)
            if previous is None or (review.get("submittedAt") or "") >= (previous.get("submittedAt") or ""):
                latest[author] = review
        return any(review["state"] == "CHANGES_REQUESTED"
                   and (review.get("commit") or {}).get("oid") == head
                   for review in latest.values())

    def approved_this_head(self, card, head):
        # Historical actions/cache cannot resurrect an approval QQ dismissed on GitHub.
        approved = head in github.approved_heads(card["repo"], card["number"], self.login)
        decision = dict(self.store.get_pr(card["key"])["decision"])
        if approved:
            decision["approved_head"] = head
        else:
            decision.pop("approved_head", None)
            decision["merge_ready"] = False
        if decision != card["decision"]:
            self.store.update_pr(card["key"], decision=decision)
        return approved

    def latest_findings_review(self, key):
        done = [a for a in self.store.actions(key) if a["event"] == "review:findings" and a["phase"] == "done"]
        return done[-1]["result"] if done else None

    def findings_review(self, key, head):
        done = [a for a in self.store.actions(key)
                if a["head"] == head and a["event"] == "review:findings" and a["phase"] == "done"]
        return done[0]["result"] if done else None

    CI_FAMILIES = ("tag-and-rerun", "rerun-test", "rerun-failed", "rerun-cancelled")
    RETRY_LIMIT = 3            # sends of one CI command family per head
    RETRY_GAP = 30 * 60        # wait before the next send; doubles each time
    RERUN_TIMEOUT = 2 * 3600   # a rerun that never produced a new result

    def ci_attempts(self, key, head, family):
        """Done sends of one CI command family on this head, oldest first.
        Matches both `ci:<family>#n` and the older un-numbered event names."""
        return sorted((a for a in self.store.actions(key)
                       if a["head"] == head and a["phase"] == "done"
                       and re.match(rf"ci:{family}(#|:|$)", a["event"])),
                      key=lambda a: a["ts"])

    def retry_due(self, attempts, now):
        return len(attempts) < self.RETRY_LIMIT and (
            not attempts or now - attempts[-1]["ts"] >= self.RETRY_GAP * 2 ** (len(attempts) - 1))

    def send_ci(self, card, head, family, command, why, attempts, by="agent"):
        suggestion = {"family": family, "command": command, "why": why,
                      "event": f"ci:{family}#{len(attempts) + 1}"}
        return self.run_command(card["key"], head, suggestion, by=by)

    def handle_ci(self, card, snap, merge_gate=True):
        """Run CI to completion without you: send the CI commands, retry what
        did not run with backoff, approve held fork runs, triage failures and
        tell the author about the ones that are theirs. For an approved head it
        also decides merge readiness. In live mode nothing here waits on you."""
        key, head, now = card["key"], snap["headRefOid"], time.time()
        previous = card["ci"] or {}
        triage_record = previous.get("triage") if previous.get("head") == head else None
        sent = {family: self.ci_attempts(key, head, family) for family in self.CI_FAMILIES}
        exhausted = {f for f, attempts in sent.items()
                     if len(attempts) >= self.RETRY_LIMIT and now - attempts[-1]["ts"] >= self.RETRY_GAP}

        targeted_status, extra_failures = None, []
        if sent["rerun-test"]:
            since = datetime.fromtimestamp(sent["rerun-test"][-1]["ts"] - 120, timezone.utc)
            targeted_status = github.rerun_test_status(card["repo"], card["number"],
                                                       since.strftime("%Y-%m-%dT%H:%M:%SZ"))
            if targeted_status["state"] == "failed":
                finished = targeted_status.get("updated_at", "")
                for run_url in targeted_status["runs"]:
                    extra_failures += [dict(job, name=ci.TARGETED_PREFIX + job["name"],
                                            completed_at=job.get("completed_at") or finished)
                                       for job in github.run_failed_jobs(run_url)]
                if not extra_failures:  # no job list to read: the run as a whole failed
                    extra_failures = [{"name": ci.TARGETED_PREFIX + "rerun-test", "url": targeted_status["url"],
                                       "conclusion": "FAILURE", "completed_at": finished}]
        assessment = ci.assess(card, snap, triage_record, extra_failures, exhausted,
                               waived=card["decision"].get("ci_waived") or ())
        targeted_state = (targeted_status or {}).get("state")
        if targeted_state == "missing" and "rerun-test" not in exhausted:
            for suggestion in assessment["suggestions"]:
                if suggestion["family"] == "rerun-test":  # the bot never answered (a queued run is not stuck)
                    suggestion.update(retry=True, why="the bot never answered the last /rerun-test; sending again")
        if targeted_state == "missing" and "rerun-test" in exhausted:
            assessment["notes"].append(f"targeted tests never reported back after {self.RETRY_LIMIT} sends")

        manual = []
        for suggestion in assessment["suggestions"]:
            family = suggestion["family"]
            attempts = sent[family]
            if assessment["conflict"]:  # GitHub runs no PR CI on a conflicting head
                suggestion.update(event=f"ci:{family}#{len(attempts) + 1}", attempts=len(attempts),
                                  state="done" if attempts else "todo", sent_at=attempts[-1]["ts"] if attempts else 0)
                continue
            if family == "rerun-test" and not suggestion.get("retry"):
                due = not attempts
            elif family == "rerun-failed":
                newest = max((epoch(f.get("completed_at") or "") for f in assessment["retryable"]), default=0)
                took_effect = not attempts or newest > attempts[-1]["ts"] or now - attempts[-1]["ts"] >= self.RERUN_TIMEOUT
                due = len(attempts) < self.RETRY_LIMIT and took_effect
            else:
                due = self.retry_due(attempts, now)
            suggestion.update(event=f"ci:{family}#{len(attempts) + 1}", attempts=len(attempts),
                              state="todo" if due or not attempts else "done",
                              sent_at=attempts[-1]["ts"] if attempts else 0)
            if not due:
                continue
            if not self.auto_allowed(card, family):
                manual.append(suggestion)
            elif self.run_command(key, head, suggestion, by="agent"):
                suggestion.update(state="done", attempts=len(attempts) + 1, sent_at=now)
        sent = {family: self.ci_attempts(key, head, family) for family in self.CI_FAMILIES}
        if sent["rerun-test"] and targeted_status is None:
            targeted_state = "running"  # sent just now
        assessment.update(triage=triage_record, targeted=targeted_status,
                          attempts={f: len(a) for f, a in sent.items() if a})
        self.store.update_pr(key, ci=assessment)
        if assessment["needs_triage"]:  # new failures: the rules pass is free
            self.submit(key, self.job_ci_triage, key, snap, assessment["failures"])
        elif (triage_record or {}).get("pending") and assessment["status"] == "failed" \
                and not self.agent_gate(card):  # the agent reads what the rules left
            self.submit(key, self.job_ci_triage, key, snap, assessment["failures"])
        if not merge_gate:
            return  # no approval yet: CI is run and shown, not judged

        status, counts = assessment["status"], assessment["checks"]["counts"]
        related = [f for f in assessment["blocking"] if f["classification"] == "related"]
        unknown = [f for f in assessment["blocking"] if f["classification"] != "related"]
        note = {
            "running": f"CI running ({counts['success']} passed, {counts['failure']} failed, {counts['pending']} pending)",
            "not_started": "CI has not started",
            "gated": "CI is held by the run-ci gate",
            "cancelled": f"{counts['cancelled']} job(s) cancelled",
            "failed": f"{counts['failure']} CI failure(s)",
            "green": "CI passed",
        }[status]
        held = (self.approve_held_runs(card, snap)
                if status in ("running", "not_started", "gated") and not assessment["conflict"] else None)
        targeted_waiting = (any(sg["family"] == "rerun-test" for sg in assessment["suggestions"])
                            and "rerun-test" not in exhausted
                            and (not sent["rerun-test"] or targeted_state in ("running", "missing")))
        merge_ready = False

        if assessment["conflict"] and self.try_fix(card, snap, "conflict"):
            pass  # the agent merges main and resolves the conflicts itself
        elif assessment["conflict"]:
            self.store.set_stage(key, "needs_author", "approved; merge conflict with main")
            self.author_note(key, head, "github:bounce:conflict",
                             "Could you rebase this onto the latest main? It conflicts with main now.")
        elif related and self.try_fix(card, snap, "ci", [
                {"id": f"C{i + 1}", "job": f["name"], "url": f.get("url", ""), "evidence": f.get("evidence", "")}
                for i, f in enumerate(related[:5])]):
            pass  # the agent fixes what the logs point at
        elif related:
            names = ", ".join(f["name"].split(" / ")[-1] for f in related[:3])
            self.store.set_stage(key, "needs_author", f"CI failures related to the change ({names}); author pinged")
            self.author_note(key, head, f"github:ci-related:{assessment['signature']}",
                             self.ci_note(card, head, related, related=True))
            self.slack_reply(key, head, "ci_related", names=names)
        elif held == "workflow-change":
            self.set_ready(card, "ci", "fork CI runs wait for approval and the PR edits .github/; approve them yourself")
        elif assessment["needs_triage"]:
            self.store.set_stage(key, "approved", note + ", agent is reading the logs")
        elif status == "running":
            self.store.set_stage(key, "approved", note + (f", approved {held} held run(s)" if held else ""))
        elif unknown:
            self.handle_unknown_failures(card, head, unknown, sent, now, assessment["signature"])
        elif manual:
            self.set_ready(card, "ci", note + "; send " + ", ".join(sg["command"].split()[0] for sg in manual))
        elif not assessment["passed"] or targeted_waiting:
            retrying = [f"{sg['command'].split()[0]} ({len(sent[sg['family']])}/{self.RETRY_LIMIT})"
                        for sg in assessment["suggestions"]
                        if sg["family"] != "rerun-test" and sg["family"] not in exhausted and sent[sg["family"]]]
            if retrying and not assessment["passed"]:
                reason = note + ", agent retrying: " + ", ".join(retrying)
            elif targeted_waiting:
                reason = note + ", targeted tests running"
            else:
                reason = note + f", still not passing after {self.RETRY_LIMIT} retries; the agent keeps watching"
            self.store.set_stage(key, "approved", reason)
        else:
            merge_ready = card["review_decision"] == "APPROVED"
            outcome = self.try_auto_merge(card, snap) if merge_ready else None
            if outcome == "merged":
                return
            if not merge_ready:
                self.store.set_stage(key, "approved", "CI passed, GitHub still needs a code-owner approval")
            elif outcome is None:  # auto-merge off: the merge is your click
                self.store.set_stage(key, "approved", "ready to merge")
                self.notify_once(key, head, "notify:mergeable", f"Ready to merge: {card['title']}")
        card = self.store.get_pr(key)
        merge_ready = merge_ready and card["stage"] == "approved"
        decision = {k: v for k, v in card["decision"].items() if not (k == "ready_kind" and card["stage"] != "ready")}
        self.store.update_pr(key, decision=dict(decision, merge_ready=merge_ready,
                                                merge_notes=assessment["notes"] if merge_ready else []))
        self.slack_reply(key, head, "approved")

    MERGE_HOLD = re.compile(r"\b(wip|do[- ]?not[- ]?merge|dnm|on[- ]hold|hold)\b", re.I)

    MERGE_STUCK_AFTER = 3  # failed merges in a row before it needs you

    def try_auto_merge(self, card, snap):
        """Merge a PR that meets the merge bar, as you. Guarded by live mode and
        policy, a daily budget, WIP / do-not-merge markers, and a backoff after a
        failed attempt; merge() re-checks the head and CI right before merging.
        Returns None when auto-merge is off, "merged", or "waiting" (stage set)."""
        if not (is_live(self.config) and self.policy["auto_merge"]):
            return None
        key, head = card["key"], snap["headRefOid"]
        labels = [label["name"] for label in snap.get("labels") or []]
        if snap.get("isDraft") or self.MERGE_HOLD.search(snap.get("title") or "") \
                or any(self.MERGE_HOLD.search(label) for label in labels):
            self.store.set_stage(key, "approved", "ready to merge; held by a WIP / do-not-merge marker")
            return "waiting"
        now = time.time()
        if self.store.count_events("merge", now - 86400, prefix="agent merged") \
                >= self.policy["max_auto_merges_per_day"]:
            self.store.set_stage(key, "approved", "ready to merge; daily auto-merge budget used")
            return "waiting"
        next_slot = self.store.last_event_ts("merge", prefix="agent merged") \
            + 60 * self.policy["min_merge_interval_minutes"]
        if now < next_slot:  # one auto-merge at a time into main, spaced out
            self.store.set_stage(key, "approved", "ready to merge; next auto-merge slot at "
                                 + time.strftime("%H:%M", time.localtime(next_slot)))
            return "waiting"
        failure = card["decision"].get("merge_failure") or {}
        if failure.get("head") == head and \
                time.time() - failure["at"] < min(600 * 2 ** (failure["count"] - 1), 7200):
            self.merge_waiting(card, failure)
            return "waiting"
        try:
            self.merge(key, by="agent")
            return "merged"
        except (github.GitHubError, ValueError) as exc:
            lines = str(exc).strip().splitlines()
            error = (lines[-1] if lines else "unknown error")[:200]
            count = failure.get("count", 0) + 1 if failure.get("head") == head else 1
            unlinked = []
            if isinstance(exc, github.GitHubError) and ("--admin" in str(exc) or "prohibits" in str(exc)):
                try:  # blocked by the repo's rules: commits not linked to an account are the usual cause
                    unlinked = github.unlinked_commits(card["repo"], card["number"])
                except github.GitHubError:
                    pass
            self.store.update_pr(key, decision=dict(self.store.get_pr(key)["decision"], merge_failure={
                "head": head, "at": time.time(), "count": count, "error": error, "unlinked": unlinked}))
            self.store.add_event(key, "error", f"auto-merge failed: {error}")
            self.merge_waiting(self.store.get_pr(key), {"count": count, "error": error, "unlinked": unlinked})
            return "waiting"

    def merge_waiting(self, card, failure):
        """A failed merge retries on its own; only a repeated failure needs you.
        Commits not linked to a GitHub account are the author's to fix."""
        if failure.get("unlinked"):
            key, head = card["key"], card["head_sha"]
            commits = ", ".join(f"`{oid[:8]}`" for oid in failure["unlinked"][:5])
            self.store.set_stage(key, "needs_author", f"GitHub blocks the merge: commit {commits} is not linked to "
                                 "a GitHub account; author asked")
            self.author_note(key, head, "github:unlinked-commits", (
                f"@{card['author']} this is approved, but GitHub won't merge it: commit {commits} isn't linked "
                "to your GitHub account, and this repo needs an extra approval for that. Could you add that commit "
                "email to your account (Settings → Emails), or recommit with your GitHub email and push? "
                "It'll merge automatically after that."))
        elif failure["count"] >= self.MERGE_STUCK_AFTER:
            self.set_ready(card, "merge", f"auto-merge failed {failure['count']} times: {failure['error']}")
        else:
            self.store.set_stage(card["key"], "approved", "ready to merge; auto-merge failed, retrying: " + failure["error"])

    # ---- agent fixes: what the author would otherwise be waited on for ------
    FIX_TASKS = {"conflict": "resolve the merge conflicts with main",
                 "findings": "fix the verified review findings",
                 "ci": "fix the CI failures caused by this change"}

    def try_fix(self, card, snap, kind, issues=()):
        """Fix it instead of waiting on the author. False when the policy, a
        cap, your hand-off to the author, an earlier attempt on this head or a
        branch closed to maintainer edits says no; the caller then asks the
        author as before. True when a fix runs or waits for its slot: one fix at
        a time, pushes spaced out."""
        if not (is_live(self.config) and self.policy["auto_fix"]):
            return False
        key, head = card["key"], snap["headRefOid"]
        if card["decision"].get("waiting_author_head") == head or (kind != "conflict" and not issues):
            return False
        if key in self.inflight:
            return True
        actions = self.store.actions(key)
        if any(a["head"] == head and a["event"] == f"fix:{kind}" for a in actions):
            return False  # one attempt per head
        if sum(1 for a in actions if a["event"].startswith("fix:") and (a["result"] or {}).get("pushed")) \
                >= self.policy["max_agent_fixes_per_pr"]:
            return False
        running = sum(1 for job in self.inflight.values() if job == "job_fix")
        now = time.time()
        if self.store.count_events("fix", now - 86400) + running >= self.policy["max_agent_fixes_per_day"]:
            return False
        if not github.push_target(card["repo"], card["number"]):
            # Checked before taking a slot: nothing to try on this head.
            if self.store.claim_action(key, head, f"fix:{kind}"):
                self.store.finish_action(key, head, f"fix:{kind}", True, {
                    "pushed": False, "reason": "the author does not allow edits from maintainers"})
                self.store.add_event(key, "decision", "agent can't push to this branch (maintainer edits are off); "
                                     "asking the author")
            return False
        next_slot = self.store.last_event_ts("fix", prefix="agent pushed") \
            + 60 * self.policy["min_fix_interval_minutes"]
        if running or now < next_slot:
            when = "after the running fix" if running else time.strftime("%H:%M", time.localtime(next_slot))
            self.store.set_stage(key, "queued", f"agent will {self.FIX_TASKS[kind]}; next fix slot {when}")
            return True
        wait = self.agent_gate(card)
        if wait:
            self.store.set_stage(key, "queued", f"agent will {self.FIX_TASKS[kind]}; {wait}")
            return True
        self.store.set_stage(key, "reviewing", f"agent is trying to {self.FIX_TASKS[kind]}")
        self.submit(key, self.job_fix, key, snap, kind, list(issues))
        return True

    def job_fix(self, key, snap, kind, issues):
        """One attempt per head and kind. Pushed: tell the author, the new head
        is reviewed again. Not pushed: the next refresh hands it to the author."""
        head = snap["headRefOid"]
        if not self.store.claim_action(key, head, f"fix:{kind}"):
            return
        result = {"pushed": False, "reason": "failed"}
        try:
            result = self.run_fix(self.store.get_pr(key), snap, kind, issues)
        finally:
            self.store.finish_action(key, head, f"fix:{kind}", True, result)
        card = self.store.get_pr(key)
        if not result["pushed"]:
            self.store.add_event(key, "fix", f"agent could not {self.FIX_TASKS[kind]}: {result['reason']}")
            self.store.set_stage(key, "queued", f"agent could not {self.FIX_TASKS[kind]}; asking the author")
            return
        sha = result["head"]
        self.store.add_event(key, "fix", f"agent pushed {sha[:8]}: {result['summary']}",
                             {"url": f"{card['url']}/commits/{sha}"})
        titles = {issue["id"]: issue.get("title") or issue.get("job") or issue["id"] for issue in issues}
        summary = plain(result["summary"]).rstrip(".")
        lines = [f"@{card['author']} I pushed `{sha[:8]}` to your branch: {summary}. Please pull before pushing again."]
        if kind != "conflict" and result["skipped"]:
            lines += ["", "Still open:"]
            lines += [f"- {plain(titles.get(s['id'], s['id']))}: {plain(s['reason'])}" for s in result["skipped"]]
        self.author_note(key, head, f"github:fix:{kind}", "\n".join(lines))
        self.store.set_stage(key, "queued", f"agent pushed {sha[:8]} ({result['summary']}); reviewing the new head")

    def run_fix(self, card, snap, kind, issues):
        target = github.push_target(card["repo"], card["number"])
        if not target:
            return {"pushed": False, "reason": "the author does not allow edits from maintainers"}
        if kind == "ci":
            for issue in issues:
                issue["log_tail"] = github.failed_job_log(issue["url"], 120)
        base = snap["baseRefName"]
        ws = agent.Workspace(self.config, card["repo"], card["number"], snap["headRefOid"], base)
        try:
            ws.prepare(snap)
            checkout = fixer.Checkout(ws.worktree, self.login, self.noreply_email())
            outcome = {"fixed": [], "skipped": [], "summary": ""}
            if kind == "conflict":
                conflicted = checkout.merge(f"origin/{base}")
                if conflicted:
                    outcome = agent.fix(self.config, ws.worktree, card["repo"], card["number"],
                                        f"Resolve the conflicts from merging {base} into this PR.",
                                        [{"id": f, "file": f} for f in conflicted])
                    left = checkout.unresolved(conflicted)
                    if left:
                        checkout.abort_merge()
                        return {"pushed": False, "reason": "conflicts left in " + ", ".join(left[:3])}
                    checkout.tidy(conflicted)
                    checkout.compile_check(conflicted)
                    checkout.finish_merge(conflicted)
                files = ", ".join(f"`{f}`" for f in conflicted[:5])
                outcome["summary"] = f"merged {base} and resolved the conflicts in {files}" if conflicted \
                    else f"merged {base}"
            else:
                task = self.FIX_TASKS[kind][0].upper() + self.FIX_TASKS[kind][1:] + "."
                outcome = agent.fix(self.config, ws.worktree, card["repo"], card["number"], task, issues)
                changed = checkout.changed_files()
                if not changed:
                    reasons = "; ".join(s["reason"] for s in outcome["skipped"][:2])
                    return {"pushed": False, "reason": reasons or "no change the agent was confident in"}
                checkout.tidy(changed)
                changed = checkout.changed_files()
                checkout.compile_check(changed)
                checkout.commit(fixer.commit_message(outcome["summary"]), changed)
            sha = checkout.head()
            checkout.push(target["url"], target["branch"])
        finally:
            ws.cleanup()
        return {"pushed": True, "head": sha, "summary": outcome["summary"].strip().rstrip("."),
                "fixed": outcome["fixed"], "skipped": outcome["skipped"]}

    def noreply_email(self):
        """Your GitHub noreply address: agent commits count as attributed to you."""
        if not getattr(self, "_noreply", None):
            user = github.gh_json(["api", "user"])
            self._noreply = f"{user['id']}+{user['login']}@users.noreply.github.com"
        return self._noreply

    def handle_unknown_failures(self, card, head, failures, sent, now, signature):
        """A failure nobody could attribute gets one fresh run; if it fails
        again, the author is asked to look, but only with a log line to show.
        Without a readable log it is rerun like infra (bounded, backing off)
        and then watched. Never parked on you."""
        key = card["key"]
        family_of = lambda f: "rerun-test" if f["name"].startswith(ci.TARGETED_PREFIX) else "rerun-failed"  # noqa: E731
        todo, waiting, again, no_log_retry, waiting_agent = set(), False, [], 0, False
        for failure in failures:
            attempts = sent[family_of(failure)]
            finished = epoch(failure.get("completed_at") or "")
            failed_again = any(a["ts"] < finished for a in attempts)
            if len(attempts) >= self.RETRY_LIMIT or (failed_again and evidence_line(failure)):
                again.append(failure)  # it already failed after a rerun
            elif failed_again and failure.get("pending_agent"):
                waiting_agent = True  # one rerun done; the agent reads the log in its next window
            elif failed_again:  # no log to go on: rerun again rather than ask the author
                no_log_retry = max(no_log_retry, len(attempts))
                if self.retry_due(attempts, now):
                    todo.add(family_of(failure))
                else:
                    waiting = True
            elif attempts and now - attempts[-1]["ts"] < self.RERUN_TIMEOUT:
                waiting = True
            else:
                todo.add(family_of(failure))
        review = card["review"] or {}
        for family in sorted(todo):
            if not self.auto_allowed(card, family):
                continue
            command = "/rerun-failed-ci" if family == "rerun-failed" else \
                "/rerun-test " + " ".join(review.get("targeted_tests") or [])
            self.send_ci(card, head, family, command, "unattributed failure: one fresh run first", sent[family])
        readable = [f for f in again if evidence_line(f)]
        if waiting_agent and not (todo or waiting or readable):
            self.store.set_stage(key, "approved", f"{len(failures)} CI failure(s) still failing after a rerun; "
                                 + (self.agent_gate(card) or "the agent is reading the logs"))
        elif no_log_retry and (todo or waiting):
            done = no_log_retry + (1 if todo else 0)
            self.store.set_stage(key, "approved", f"{len(failures)} CI failure(s) without a readable log, "
                                 f"agent rerunning ({done}/{self.RETRY_LIMIT})")
        elif todo or waiting:
            self.store.set_stage(key, "approved", f"{len(failures)} unattributed CI failure(s), rerunning once")
        elif not readable:  # nothing concrete to show the author; don't guess at them
            self.store.set_stage(key, "approved", f"{len(again)} CI failure(s) without a readable log; "
                                 "the agent keeps watching")
        else:
            again = readable
            names = ", ".join(f["name"].split(" / ")[-1] for f in again[:3])
            self.store.set_stage(key, "needs_author", f"CI still failing after a rerun ({names}); author pinged")
            self.author_note(key, head, f"github:ci-unknown:{signature}",
                             self.ci_note(card, head, again, related=False))

    def approve_held_runs(self, card, snap):
        """Approve the fork workflow runs GitHub holds for maintainer approval,
        on lanes this change needs. The code itself is already approved."""
        if not (is_live(self.config) and self.policy["auto_ci"]):
            return None
        head = snap["headRefOid"]
        try:
            runs = github.runs_awaiting_approval(card["repo"], head)
        except github.GitHubError:
            return None
        platforms = ci.pr_platforms(snap) | {"base"}
        runs = [r for r in runs if (ci.lane_platform(r["workflow"] + " / ") or "base") in platforms]
        if not runs:
            return None
        if any((f.get("path") or "").startswith(".github/") for f in snap.get("files") or []):
            return "workflow-change"
        approved = 0
        for run in runs:
            event = f"ci:approve-run:{run['id']}"
            if not self.store.claim_action(card["key"], head, event):
                continue
            try:
                github.approve_run(card["repo"], run["id"])
            except github.GitHubError as exc:
                self.store.finish_action(card["key"], head, event, False)
                self.store.add_event(card["key"], "error", f"approving held run {run['workflow']} failed: {exc}")
                continue
            self.store.finish_action(card["key"], head, event, True, {"url": run["url"]})
            self.store.add_event(card["key"], "action", f"agent approved held CI run: {run['workflow']}",
                                 {"url": run["url"]})
            approved += 1
        return approved or None

    def ci_note(self, card, head, failures, related):
        lines = [f"@{card['author']} " + (
            "these CI failures look related to this change, could you take a look?" if related else
            "these still fail after a rerun, and I can't tell from the logs whether it's this PR. "
            "Could you take a look?")]
        for failure in failures[:5]:
            evidence = evidence_line(failure)
            lines.append(f"- [`{failure['name'].split(' / ')[-1]}`]({failure.get('url', '')})"
                         + (f": `{evidence[:200]}`" if evidence else ""))
        return "\n".join(lines)

    def auto_approve_tiers(self):
        return self.policy["auto_approve_tiers"] if is_live(self.config) and self.policy["auto_approve"] \
            else ["T1", "T2"]

    def auto_approve_allowed(self):
        if not (is_live(self.config) and self.policy["auto_approve"]):
            return False
        return self.store.count_events("approve", time.time() - 86400) < self.policy["max_auto_approvals_per_day"]

    def set_ready(self, card, kind, reason):
        self.store.update_pr(card["key"], decision=dict(card["decision"], ready_kind=kind))
        self.store.set_stage(card["key"], "ready", reason)
        self.notify_once(card["key"], card["head_sha"], f"notify:ready:{kind}", f"Needs you: {card['title']}")

    def auto_allowed(self, card, family):
        if not is_live(self.config):
            return False
        family = family.removeprefix("ci:").split("#")[0].split(":")[0]
        if family == "tag-and-rerun":
            return self.policy["auto_ci"]
        if family == "rerun-test":
            return self.policy["auto_targeted_tests"]
        if family in ("rerun-failed", "rerun-cancelled"):
            return self.policy["auto_retry_failed_ci"]
        return False

    # ---- jobs (worker threads) -------------------------------------------
    # ---- agent budget: time windows and a daily token cap ------------------
    AGENT_JOBS = ("job_review", "job_ci_triage", "job_fix", "job_discuss")

    def record_usage(self, job, tokens):
        self.store.add_usage(job, tokens)

    def backfill_usage(self, runs_dir):
        """First start with metering: count today's earlier codex runs from their logs."""
        if self.store.has_usage() or not runs_dir.is_dir():
            return
        since = self.day_start()
        for run in runs_dir.iterdir():
            stderr = run / "stderr.txt"
            if stderr.exists() and stderr.stat().st_mtime >= since:
                job = run.name.rsplit("-", 2)[-2] if run.name.count("-") >= 2 else "agent"
                self.store.add_usage(job, agent.tokens_used(stderr.read_text(errors="ignore")),
                                     ts=stderr.stat().st_mtime)

    @staticmethod
    def day_start(now=None):
        return datetime.fromtimestamp(now or time.time()).replace(hour=0, minute=0, second=0,
                                                                  microsecond=0).timestamp()

    def window_starts(self, now):
        local = datetime.fromtimestamp(now)
        starts = []
        for day in (-1, 0, 1, 2):
            base = (local + timedelta(days=day)).replace(second=0, microsecond=0)
            for spec in self.config["agent"].get("windows") or []:
                hour, minute = (int(part) for part in spec.split(":"))
                starts.append(base.replace(hour=hour, minute=minute).timestamp())
        return sorted(starts)

    def window_state(self, now=None):
        """(open, next start): agent jobs start only inside a window, or a run you
        started by hand; no windows = always open."""
        now = now or time.time()
        if not self.config["agent"].get("windows"):
            return True, now
        span = 60 * self.config["agent"].get("window_minutes", 120)
        starts = self.window_starts(now)
        open_ = any(start <= now < start + span for start in starts) or self.manual_until() > now
        return open_, next(s for s in starts if s > now)

    # You can change the daily cap and start a run from the page; both are kept in the store.
    def token_budget(self):
        return int(self.store.get_kv("daily_token_budget", self.config["agent"].get("daily_token_budget") or 0))

    def set_token_budget(self, tokens):
        self.store.set_kv("daily_token_budget", int(tokens))
        self.kick(reset_sources=True)

    def manual_until(self):
        return float(self.store.get_kv("manual_window_until", 0) or 0)

    def run_now(self):
        """Open an agent window now, as long as a scheduled one."""
        if self.budget_exhausted():
            raise ValueError("today's agent token budget is used; raise the daily limit first")
        self.store.set_kv("manual_window_until", time.time() + 60 * self.config["agent"].get("window_minutes", 120))
        self.kick(reset_sources=True)  # every card is looked at again right away

    def stop_run(self):
        self.store.set_kv("manual_window_until", 0)

    def budget_exhausted(self):
        """Not even one more job fits under today's cap."""
        budget = self.token_budget()
        return bool(budget) and self.store.tokens_since(self.day_start()) + \
            self.config["agent"].get("token_reserve", 150000) > budget

    def running_agent_jobs(self):
        return sum(1 for job in self.inflight.values() if job in self.AGENT_JOBS) + bool(self.translating)

    def agent_gate(self, card=None, window=True):
        """'' when an agent job may start now, else why not. A card you just
        asked about (rereview, add, restore) skips the window, never the budget."""
        now, cfg = time.time(), self.config["agent"]
        budget = self.token_budget()
        if budget:
            used = self.store.tokens_since(self.day_start(now))
            if used + (self.running_agent_jobs() + 1) * cfg.get("token_reserve", 150000) > budget:
                tomorrow = self.day_start(now) + 86400
                starts = [s for s in self.window_starts(now) if s >= tomorrow] if cfg.get("windows") else [tomorrow]
                return (f"today's agent token budget is used ({used / 1e6:.1f}M of {budget / 1e6:g}M); "
                        f"next agent run {datetime.fromtimestamp(starts[0]).strftime('%m-%d %H:%M')}")
        if window and not (card and card["decision"].get("run_now_until", 0) > now):
            open_, next_start = self.window_state(now)
            if not open_:
                return f"waiting for the next agent window ({datetime.fromtimestamp(next_start).strftime('%H:%M')})"
        return ""

    def agent_status(self):
        now, cfg = time.time(), self.config["agent"]
        open_, next_start = self.window_state(now)
        tomorrow = self.day_start(now) + 86400
        resume = [s for s in self.window_starts(now) if s >= tomorrow] if cfg.get("windows") else [tomorrow]
        return {"windows": cfg.get("windows") or [], "open": open_,
                "next": next_start if cfg.get("windows") else 0,
                "manual_until": self.manual_until() if self.manual_until() > now else 0,
                "tokens_today": self.store.tokens_since(self.day_start(now)),
                "budget": self.token_budget(), "default_budget": cfg.get("daily_token_budget") or 0,
                "exhausted": self.budget_exhausted(), "resume_at": resume[0],
                "running": self.running_agent_jobs(),
                "blocked": self.agent_gate(window=False)}

    def submit(self, key, fn, *args):
        with self.lock:
            if key in self.inflight:
                return
            self.inflight[key] = fn.__name__

        def wrapped():
            decision = {}
            try:
                if fn.__name__ in self.AGENT_JOBS and self.closed_before_start(key):
                    decision = {"refresh": True}
                    return
                fn(*args)
                decision = {"refresh": True, "error_count": 0}
                self.status.pop("agent_blocked", None)
            except Exception as exc:
                errors = self.store.get_pr(key)["decision"].get("error_count", 0) + 1
                # refresh_due backs off: 5 min, doubling, at most hourly.
                decision = {"error_count": errors}
                self.store.set_stage(key, "error", f"failure #{errors}: {self.agent_blocked(exc) or str(exc)[:300]}")
                self.store.add_event(key, "error", traceback.format_exc()[-1200:])
            finally:
                with self.lock:
                    self.inflight.pop(key, None)
                self.store.update_pr(key, decision=dict(self.store.get_pr(key)["decision"], **decision))
                self.wake.set()

        self.executor.submit(wrapped)

    def closed_before_start(self, key):
        """A job can wait behind max_concurrent for a long time, and tick() does not
        refresh a card while its key is in inflight. Check the PR is still open
        right before an agent job starts, so a PR merged or closed meanwhile is not
        reviewed for nothing; the refresh this triggers closes the card."""
        card = self.store.get_pr(key)
        if card is None:
            return False
        try:
            state = github.fetch_pr(card["repo"], card["number"]).get("state")
        except Exception:  # can't tell: run the job as before
            return False
        if state == "OPEN":
            return False
        self.store.add_event(key, "skip", f"PR is {str(state).lower()} before the agent job started; job skipped")
        return True

    def agent_blocked(self, exc):
        """An account-level stop (usage limit / spend cap on every codex
        account) is shown once at the top of the page instead of as a long
        error on every card."""
        if not agent.LIMIT_HIT.search(str(exc)):
            return ""
        self.status["agent_blocked"] = {"reason": "every codex account is out of quota", "at": time.time()}
        return "every codex account is out of quota, retrying automatically"

    def carry_over(self, card, snap):
        """A new head whose PR diff is unchanged (only main merged in) keeps its
        review: no agent run. The git check is free and cached per head."""
        review, head = card["review"] or {}, snap["headRefOid"]
        if not (review.get("diff_fp") and card["review_head"]) or card["decision"].get("full_review"):
            return False
        cached = card["decision"].get("head_fp") or ["", ""]
        if cached[0] == head:
            fp = cached[1]
        else:
            try:
                fp = agent.Workspace(self.config, card["repo"], card["number"], head,
                                     snap["baseRefName"]).diff_fingerprint()
            except (agent.AgentError, AttributeError, OSError, subprocess.SubprocessError):
                return False
            self.store.update_pr(card["key"], decision=dict(card["decision"], head_fp=[head, fp]))
        if fp != review["diff_fp"]:
            return False
        self.store.update_pr(card["key"], review=self.carried_review(card, head), review_head=head)
        self.store.add_event(card["key"], "decision", f"review carried over to {head[:8]}: the PR's own diff is "
                             f"unchanged since {card['review_head'][:8]}, only main was merged in")
        return True

    def carried_review(self, card, head, **extra):
        """The review moved to a new head: findings already posted are not posted again."""
        review, published = card["review"], self.findings_review_any(card["key"])
        findings = [dict(f, raised_before=bool(f.get("raised_before") or published)) for f in review["findings"]]
        return dict(review, findings=findings, head=head, **extra)

    def delta_base(self, card):
        """The reviewed head a follow-up review starts from; None means a full review."""
        review = card["review"] or {}
        if "findings" not in review or not card["review_head"] or card["decision"].get("full_review"):
            return None
        return card["review_head"]

    def job_review(self, key, snap, tier, since=None):
        head = snap["headRefOid"]
        self.store.set_stage(key, "reviewing", f"agent reviewing ({tier}, {self.config['agent']['runner']})"
                             + (f", follow-up since {since[:8]}" if since else ""))
        card = self.store.get_pr(key)
        workspace = agent.Workspace(self.config, card["repo"], card["number"], head, snap["baseRefName"])
        skill_dir, skill_commit = (self.skills.ensure() if self.skills
                                   else (self.config["agent"]["review_skill_dir"], ""))
        earlier = (card["review"] or {}).get("findings") or [] if since else []
        published = self.findings_review_any(key)
        try:
            workspace.prepare(snap)
            ranges = new_side_ranges((workspace.ctx / "diff.patch").read_text())
            delta = workspace.prepare_delta(since) if since and hasattr(workspace, "prepare_delta") else None
            if delta is not None and not delta.strip():  # nothing in the PR's files changed
                review = self.carried_review(card, head, at=time.time(), diff_fp=getattr(workspace, "fingerprint", ""))
                self.store.update_pr(key, review=review, review_head=head)
                self.store.add_event(key, "decision", f"review carried over to {head[:8]}: no change in the PR's "
                                     f"files since {since[:8]}")
                return
            if delta is not None:
                previous = [{name: f.get(name) for name in ("title", "severity", "kind", "category", "path", "line",
                                                            "scenario", "comment")} for f in earlier]
                result, review_info = agent.review_delta(self.config, workspace, tier, skill_dir, since, previous)
            else:
                since = None
                result, review_info = agent.review(self.config, workspace, tier, skill_dir, skill_commit)
            findings = [dict(f, anchored=anchored(f, ranges)) for f in result["findings"]]
            # Findings carried over from the earlier review keep their verification; only new ones are checked.
            old = {f["title"]: f for f in earlier} if since else {}
            carried = [dict(f, verify=old[f["title"]].get("verify", "unverified"),
                            verify_reason=old[f["title"]].get("verify_reason", ""),
                            raised_before=bool(old[f["title"]].get("raised_before") or published))
                       for f in findings if f["title"] in old]
            fresh = [f for f in findings if f["title"] not in old]
            verification, verify_info = agent.verify(self.config, workspace, fresh)
            kept, dropped = agent.apply_verification(fresh, verification)
            kept = carried + kept
            # Asking again for evidence someone already asked for is noise, not review.
            repeats = [f for f in kept if f.get("raised_before") and not agent.is_risk(f)]
            kept = [f for f in kept if f not in repeats]
            dropped += [dict(f, verify="rejected", verify_reason="repeats an earlier evidence request")
                        for f in repeats]
            verdict = agent.effective_verdict(result, kept)
        finally:
            workspace.cleanup()
        review = dict(result, findings=kept, dropped=dropped, verdict=verdict,
                      agent_verdict=result["verdict"], head=head, at=time.time(),
                      skill_commit=skill_commit, runs=[review_info, verify_info],
                      diff_fp=getattr(workspace, "fingerprint", ""), delta_since=since or "")
        if since:  # a follow-up keeps the full picture from the earlier review
            earlier_review = card["review"] or {}
            for name in ("mermaid", "corpus_synthesis"):
                review[name] = review.get(name) or earlier_review.get(name, "")
            review["summary"] = (earlier_review.get("summary", "") + f"\nSince {since[:8]}:\n"
                                 + result.get("summary", "")).strip()
        self.store.update_pr(key, review=review, review_head=head)
        decision = self.store.get_pr(key)["decision"]
        if decision.get("full_review"):
            self.store.update_pr(key, decision=dict(decision, full_review=False))
        self.store.add_event(key, "review",
                             (f"agent follow-up review since {since[:8]} done: " if since else "agent review done: ")
                             + f"{verdict}, {len(kept)} finding(s) kept, {len(dropped)} rejected by the verifier"
                             + (f", skill {skill_commit[:8]}" if skill_commit and not since else ""),
                             {"seconds": review_info.get("seconds")})

    def job_ci_triage(self, key, snap, failures):
        """Rules first, free; the agent reads only what they leave, and only when
        the agent gate is open. What it could not read yet waits as `pending`."""
        head = snap["headRefOid"]
        card = self.store.get_pr(key)
        # Every failing leg gets its log, at most 20: one leg per job first,
        # then the other legs. Downloaded in parallel because large logs are slow.
        first = {}
        for failure in failures:
            workflow, _, job = failure["name"].rpartition(" / ")
            first.setdefault(f"{workflow} / {job.split(' (')[0]}", failure["name"])
        order = list(first.values()) + [f["name"] for f in failures if f["name"] not in first.values()]
        wanted = [f for f in failures if f["name"] in set(order[:20])]
        with ThreadPoolExecutor(max_workers=4) as pool:
            logs = dict(zip([f["name"] for f in wanted],
                            pool.map(lambda f: github.failed_job_log(f["url"], 150), wanted)))
            steps = dict(zip([f["name"] for f in wanted],
                             pool.map(lambda f: github.failed_step(f.get("url", "")), wanted)))
        with_logs = [dict(f, log_tail=logs.get(f["name"], NO_LOG), failed_step=steps.get(f["name"]))
                     for f in failures]
        ruled = ci.rule_triage(with_logs)
        rest = [f for f in with_logs if f["name"] not in {r["name"] for r in ruled}]
        result, info, pending = {"failures": list(ruled), "summary": ""}, {}, []
        if rest and not self.agent_gate(card):
            workspace = agent.Workspace(self.config, card["repo"], card["number"], head, snap["baseRefName"])
            try:
                workspace.prepare(snap)
                read, info = agent.triage_ci(self.config, workspace, [
                    {k: v for k, v in f.items() if k != "failed_step"} for f in rest],
                    (card["review"] or {}).get("related_tests") or [])
            finally:
                workspace.cleanup()
            result["failures"] += read.get("failures", [])
            result["summary"] = read.get("summary", "")
        elif rest:
            pending = [f["name"] for f in rest]
        if ruled and not result["summary"]:
            kinds = sorted({r["classification"] for r in ruled})
            result["summary"] = (f"{len(ruled)} failure(s) classified by rules ({', '.join(kinds)})"
                                 + (f"; {len(pending)} left for the agent's next window" if pending else ""))
        elif pending:
            result["summary"] = f"{len(pending)} failure(s) left for the agent's next window"
        card = self.store.get_pr(key)
        # Jobs whose log could not be read: nothing in them may be quoted to the author.
        no_log = sorted(f["name"] for f in with_logs if github.log_missing(f["log_tail"]))
        record = dict(result, signature=ci.signature(failures), head=head, at=time.time(), no_log=no_log,
                      pending=pending)
        self.store.update_pr(key, ci=dict(card["ci"] or {}, triage=record))
        self.store.add_event(key, "ci", f"CI triage: {result['summary']}", {"seconds": info.get("seconds")})

    # ---- external writes ------------------------------------------------
    def run_command(self, key, head, suggestion, by):
        card = self.store.get_pr(key)
        if not self.store.claim_action(key, head, suggestion["event"]):
            return False
        body = suggestion["command"]  # bare: the CI bots parse it
        try:
            url = github.comment(card["repo"], card["number"], body)
        except github.GitHubError as exc:
            self.store.finish_action(key, head, suggestion["event"], False)
            self.store.add_event(key, "error", f"sending {suggestion['command']} failed: {exc}")
            return False
        self.store.finish_action(key, head, suggestion["event"], True, {"url": url, "by": by})
        self.store.add_event(key, "action", f"{'agent' if by == 'agent' else 'you'} sent {body.splitlines()[0]}",
                             {"url": url})
        return True

    def record_draft_request(self, key, action):
        """Page COMMENT action evidence; a caller declaration, not named identity."""
        card = self.store.get_pr(key)
        if action not in ("publish_findings", "request_changes") or not card or not card["draft"]:
            return None
        head = card["head_sha"]
        if head != card["review_head"] or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ValueError("draft action is stale")
        ref = f"action:{key}:{head}:{uuid.uuid4().hex}"
        self.store.add_event(key, "draft_request", "explicit page action for Draft findings",
                             {"reference": ref, "head": head, "action": action})
        return ref

    def draft_request_source(self, card, action_ref=None):
        """Use only service records, never model text or arbitrary request fields."""
        if not card.get("draft"):
            return None
        if action_ref:
            for event in self.store.events(card["key"]):
                data = event["data"]
                if (event["kind"] == "draft_request" and data.get("reference") == action_ref
                        and data.get("head") == card["head_sha"]
                        and data.get("action") in ("publish_findings", "request_changes")):
                    return action_ref
            return None
        intent = re.compile(self.config["sources"]["slack"]["review_intent"], re.I)
        for requester in card["requesters"]:
            ref = requester.get("id", "")
            if requester.get("source") == "manual" and ref == f"manual:{card['key']}":
                return ref
            if requester.get("source") != "slack" or not re.fullmatch(r"slack:[A-Z0-9]+:[A-Z0-9]+:[0-9]+(?:\.[0-9]+)?", ref):
                continue
            mention = self.store.get_mention(ref)
            if (mention and mention["source"] == "slack" and mention["kind"] == "review"
                    and card["key"] in mention["pr_keys"] and intent.search(mention["text"])
                    and re.fullmatch(r"[UW][A-Z0-9]+", requester.get("user_id", ""))
                    and mention["author"] and mention["author"] == requester.get("author")
                    and all(mention[k] == requester.get(k) for k in ("user_id", "team_id", "channel_id"))
                    and ref.split(":")[1:3] == [mention["team_id"], mention["channel_id"]]):
                return ref
        return None

    def publish_findings(self, key, by, *, draft_request=None):
        card = self.store.get_pr(key)
        review, head = card["review"], card["review_head"]
        if head != card["head_sha"]:
            raise ValueError("the PR has a new push; these findings are stale, re-review first")
        wanted = [f for f in review["findings"]
                  if by == "human" or (f["verify"] == "confirmed" and f["severity"] != "nit" and agent.is_risk(f))]
        if not wanted:
            raise ValueError("no findings to publish")
        if self.findings_review(key, head):
            raise ValueError("findings for this head were already published")
        # Points already raised earlier are not posted again, only listed as still open.
        new = [f for f in wanted if not f.get("raised_before")]
        again = [f for f in wanted if f.get("raised_before")]
        inline = [{"path": f["path"], "line": f["line"], "side": "RIGHT", "body": finding_comment(f)}
                  for f in new if f.get("anchored")]
        loose = [f"- `{f['path']}:{f['line']}` {finding_comment(f)}" for f in new if not f.get("anchored")]
        must = sum(1 for f in wanted if f["severity"] in ("blocker", "major") and agent.is_risk(f))
        body = ("One thing to fix before this can go in." if must == 1 else
                "A few things to fix before this can go in." if must else "A few small things, nothing blocking.")
        if loose:
            body += "\n\n" + "\n".join(loose)
        if again:
            body += "\n\nStill open from my last review:\n" + "\n".join(
                f"- {plain(f['title'])} (`{f['path']}:{f['line']}`)" for f in again)
        source = self.draft_request_source(card, draft_request if by == "human" else None)
        result = self.publish_app_comment(card, head, "review:findings", body, inline,
                                          human_confirmed=by == "human", draft_request=source)
        self.store.add_event(key, "action", f"published {len(wanted)} finding(s)", result)
        return result

    def publish_app_comment(self, card, head, event, body, comments=(), *, human_confirmed=False, draft_request=None):
        """Single existing publisher, with crash-safe marker and read-only recovery."""
        key = card["key"]
        action = next((a for a in self.store.actions(key) if a["event"] == event and a["head"] == head), None)
        if action:
            if action["phase"] == "done":
                return action["result"]
            marker = action["result"].get("marker")
            evidence = action["result"]
            if not self.store.unresolved_action(key, head, event):
                done = next((a for a in self.store.actions(key)
                             if a["event"] == event and a["head"] == head and a["phase"] == "done"), None)
                if done:
                    return done["result"]
            if not marker:
                raise github.GitHubWriteUnknown("legacy running review has no marker; investigate before retry")
            try:
                result = github.reconcile_agent_review(card["repo"], card["number"], head, marker, config=self.config)
            except github.GitHubError:
                raise github.GitHubWriteUnknown("App review unresolved; readback required") from None
        else:
            marker = f"<!-- reviewinbox:{uuid.uuid4().hex} -->"
            if draft_request is None:
                draft_request = self.draft_request_source(card)
            evidence = {"marker": marker, "human_confirmed": human_confirmed}
            if draft_request:
                evidence["draft_request"] = draft_request
            if not self.store.claim_action(key, head, event, evidence):
                raise github.GitHubWriteUnknown("review action already in flight")
            try:
                options = {"draft_request": draft_request} if draft_request else {}
                result = github.submit_agent_review(card["repo"], card["number"], head, body, comments,
                    marker=marker, human_confirmed=human_confirmed, config=self.config, **options)
            except github.GitHubWriteUnknown:
                if not self.store.unresolved_action(key, head, event):
                    done = next((a for a in self.store.actions(key)
                                 if a["event"] == event and a["head"] == head and a["phase"] == "done"), None)
                    if done:
                        return done["result"]
                raise
            except github.GitHubError:
                self.store.finish_action(key, head, event, False)
                raise
        result = dict(result, marker=marker, human_confirmed=evidence.get("human_confirmed", False))
        if evidence.get("draft_request"):
            result["draft_request"] = evidence["draft_request"]
        self.store.finish_action(key, head, event, True, result)
        return result

    def publish_agent_review(self, key, note=""):
        """Publish a head-bound advisory once; QQ's formal approval is separate."""
        card = self.store.get_pr(key)
        snap = github.fetch_pr(card["repo"], card["number"])
        head = snap["headRefOid"]
        if head != card["review_head"] or head != card["head_sha"]:
            raise ValueError("the head moved; automatic review comment is stale")
        event = "review:agent-comment"
        already_done = any(a["event"] == event and a["head"] == head and a["phase"] == "done"
                           for a in self.store.actions(key))
        review = card["review"] or {}
        body = plain(note or review.get("approve_note") or review.get("summary") or "No blocking findings.")
        result = self.publish_app_comment(card, head, event, body)
        if not already_done:
            self.store.add_event(key, "agent_comment", f"automatic review comment for {head}", result)
        self.set_ready(card, "approve", "automatic review is advisory; waiting for QQ's formal approval")
        return result

    def approve(self, key, note="", by="human"):
        """Legacy automatic callers get advisory COMMENT; human callers cannot approve."""
        if by != "human":
            return self.publish_agent_review(key, note)
        raise github.ApprovalDisabled("FORMAL_APPROVAL_DISABLED")

    def merge(self, key, by="human"):
        """Your click, or the agent once the PR meets the merge bar (policy
        auto_merge). Re-checks head and CI right before merging."""
        card = self.store.get_pr(key)
        snap = github.fetch_pr(card["repo"], card["number"])
        head = snap["headRefOid"]
        if head != card["review_head"]:
            raise ValueError("the head moved; wait for the re-review")
        if not (card["ci"] or {}).get("passed") or (card["ci"] or {}).get("head") != head:
            raise ValueError("CI on the current head has not passed the merge bar")
        if not self.store.claim_action(key, head, "merge"):
            raise ValueError("merge already requested")
        try:
            github.merge(card["repo"], card["number"], head, self.config.get("merge_method", "squash"))
        except github.GitHubError:
            self.store.finish_action(key, head, "merge", False)
            raise
        self.store.finish_action(key, head, "merge", True, {"by": by})
        self.store.update_pr(key, state="MERGED", meta=dict(card["meta"] or {}, merged_by=self.login))
        self.store.add_event(key, "merge", f"{'agent' if by == 'agent' else 'you'} merged {head[:8]}",
                             {"url": card["url"]})
        self.store.set_stage(key, "closed", "merged")
        self.slack_reply(key, head, "merged")

    def sign(self, text):
        """Mark what the agent posts on GitHub as its own, like its Slack replies."""
        return self.policy["github_prefix"].format(login=self.login) + text

    def author_note(self, key, head, event, text):
        """A short PR comment telling the author why the PR is stuck (live + policy)."""
        if not (is_live(self.config) and self.policy["auto_author_notes"]):
            return
        card = self.store.get_pr(key)
        if not self.store.claim_action(key, head, event):
            return
        body = github.agent_review_body(head, self.sign(text))
        try:
            url = github.comment(card["repo"], card["number"], body)
        except github.GitHubError as exc:
            self.store.finish_action(key, head, event, False)
            self.store.add_event(key, "error", f"note to the author failed: {exc}")
            return
        self.store.finish_action(key, head, event, True, {"url": url})
        self.store.add_event(key, "action", "note to the author: " + text.splitlines()[0], {"url": url})

    def notify_once(self, key, head, event, text):
        if not self.config.get("notify_macos", True):
            return
        if not self.store.claim_action(key, head or "-", event):
            return
        # PR titles are untrusted: pass them as argv, never inside the script.
        subprocess.run(["osascript", "-e", "on run argv", "-e",
                        'display notification (item 1 of argv) with title "Review Inbox"',
                        "-e", "end run", text[:180]], capture_output=True)
        self.store.finish_action(key, head or "-", event, True)
