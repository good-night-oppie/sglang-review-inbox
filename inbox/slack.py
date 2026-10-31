"""Slack Web API client for a *user* token (xoxp-).

User scopes: search:read (find @-mentions); channels:history, groups:history,
im:history, mpim:history (thread context and whether you already replied);
im:read, mpim:read (list your DMs); users:read (names); chat:write (thread
replies to review requests)."""

import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

USER_REF = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
LINK_REF = re.compile(r"<(https?://[^|>]+)\|([^>]+)>")
SKIP_SUBTYPES = {"channel_join", "channel_leave", "bot_message", "group_join", "tombstone"}
# A short thanks / ok / emoji closes a conversation; it is not something you owe a reply to.
ACK = re.compile(r"^\W*(thanks?|thank you|thx|ty|ok(ay)?|got it|sure|sounds good|will do|noted|np|no problem|"
                 r"lgtm|cool|great|好的?|谢谢|多谢|感谢|收到|嗯+|了解|明白)\b[^?？]{0,40}$", re.I)
EMOJI_ONLY = re.compile(r"^(\s*:[\w+-]+:\s*)+$")


def is_ack(text):
    text = (text or "").strip()
    return bool(text) and (bool(EMOJI_ONLY.match(text)) or (len(text) <= 60 and bool(ACK.match(text))))

API = "https://slack.com/api/"


class SlackError(RuntimeError):
    pass


class Slack:
    def __init__(self, token):
        self.token = token
        self._user_id = None
        self.team_id = self.team = self.url = ""
        self.names = {}

    def call(self, method, params=None, post=False):
        data = urllib.parse.urlencode(params or {}).encode()
        url = API + method + ("" if post else "?" + data.decode())
        request = urllib.request.Request(url, data=data if post else None,
                                         headers={"Authorization": f"Bearer {self.token}"})
        for _ in range(3):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    time.sleep(int(exc.headers.get("Retry-After", "5")))
                    continue
                raise SlackError(f"{method}: HTTP {exc.code}") from exc
            if payload.get("ok"):
                return payload
            raise SlackError(f"{method}: {payload.get('error')}")
        raise SlackError(f"{method}: rate limited")

    def identity(self):
        if self._user_id is None:
            info = self.call("auth.test")
            self._user_id, self.team_id, self.team = info["user_id"], info["team_id"], info.get("team", "")
            self.url = info.get("url", "")
        return self._user_id

    def user_name(self, user_id):
        if user_id not in self.names:
            try:
                profile = self.call("users.info", {"user": user_id})["user"]
                self.names[user_id] = (profile.get("profile", {}).get("display_name")
                                       or profile.get("real_name") or profile.get("name") or user_id)
            except SlackError:
                self.names[user_id] = user_id
        return self.names[user_id]

    def render(self, text):
        """Readable text for the dashboard: <@U1> → @name, <url|label> → label (url)."""
        text = USER_REF.sub(lambda m: "@" + self.user_name(m.group(1)), text or "")
        return LINK_REF.sub(lambda m: f"{m.group(2)} ({m.group(1)})", text)

    def permalink(self, channel_id, ts, thread_ts=None):
        link = f"{self.url.rstrip('/')}/archives/{channel_id}/p{str(ts).replace('.', '')}"
        return link + (f"?thread_ts={thread_ts}&cid={channel_id}" if thread_ts and thread_ts != ts else "")

    def dm_conversations(self, limit):
        """Your DMs and group DMs, most recently active (Slack's priority) first."""
        result = self.call("users.conversations", {"types": "im,mpim", "exclude_archived": "true", "limit": 200})
        channels = [c for c in result.get("channels", []) if not c.get("is_user_deleted")]
        channels.sort(key=lambda c: c.get("priority", 0), reverse=True)
        return channels[:limit]

    def history(self, channel, oldest, limit=20):
        return self.call("conversations.history",
                         {"channel": channel, "oldest": f"{oldest:.6f}", "limit": limit}).get("messages", [])

    def replies(self, channel, ts, limit=100):
        return self.call("conversations.replies", {"channel": channel, "ts": ts, "limit": limit}).get("messages", [])

    def search(self, query, lookback_days, max_pages=3):
        after = (datetime.now(timezone.utc) - timedelta(days=lookback_days + 1)).strftime("%Y-%m-%d")
        matches = []
        for page in range(1, max_pages + 1):
            result = self.call("search.messages", {
                "query": f"{query} after:{after}", "count": 100, "page": page,
                "sort": "timestamp", "sort_dir": "desc"})
            block = result.get("messages", {})
            matches.extend(block.get("matches", []))
            if page >= block.get("paging", {}).get("pages", 1):
                break
        return matches

    def thread_parent_text(self, channel, thread_ts):
        result = self.call("conversations.replies",
                           {"channel": channel, "ts": thread_ts, "limit": 1, "inclusive": "true"})
        messages = result.get("messages", [])
        return messages[0].get("text", "") if messages else ""

    def reply(self, channel, thread_ts, text):
        return self.call("chat.postMessage",
                         {"channel": channel, "thread_ts": thread_ts, "text": text}, post=True)


def reacted_by(message, user_id):
    return any(user_id in reaction.get("users", []) for reaction in message.get("reactions", []))


def mention_status(mention_ts, my_id, messages):
    """Handled when you posted after the mention in its thread (or in the
    channel right after it), or reacted to it."""
    for message in messages:
        if message.get("user") == my_id and float(message.get("ts", 0)) > mention_ts:
            return "replied"
    target = next((m for m in messages if abs(float(m.get("ts", 0)) - mention_ts) < 1e-6), None)
    if target and reacted_by(target, my_id):
        return "reacted"
    return "pending"


def is_human(message):
    return not message.get("bot_id") and message.get("subtype") not in SKIP_SUBTYPES


def dm_status(my_id, messages, group=False):
    """messages: newest first. Returns (status, latest unanswered message,
    unanswered count), or None when the last word is yours. In a group DM
    people mostly talk to each other, so it only counts when one of the
    unanswered messages @-mentions you."""
    unanswered = []
    for message in messages:
        if message.get("user") == my_id:
            break
        if message.get("bot_id") or message.get("subtype") in SKIP_SUBTYPES or is_ack(message.get("text")):
            continue
        unanswered.append(message)
    if group:
        unanswered = [m for m in unanswered if f"<@{my_id}>" in (m.get("text") or "")]
    if not unanswered:
        return None
    latest = unanswered[0]
    return ("reacted" if reacted_by(latest, my_id) else "pending"), latest, len(unanswered)


def thread_ts_of(match):
    """Search results expose the thread only through the permalink query."""
    query = urllib.parse.urlparse(match.get("permalink", "")).query
    return urllib.parse.parse_qs(query).get("thread_ts", [None])[0]


def mention_record(match, team_id=""):
    channel = match.get("channel") or {}
    return {
        "id": f"slack:{team_id}:{channel.get('id', '')}:{match.get('ts')}",
        "source": "slack",
        "team_id": team_id,
        "conv": "dm" if channel.get("is_im") or channel.get("is_mpim") else "channel",
        "user_id": match.get("user", ""),
        "channel": channel.get("name") or channel.get("id", ""),
        "channel_id": channel.get("id", ""),
        "author": match.get("username") or match.get("user", ""),
        "text": match.get("text", ""),
        "permalink": match.get("permalink", ""),
        "ts": float(match.get("ts", 0)),
        "thread_ts": thread_ts_of(match) or match.get("ts"),
    }
