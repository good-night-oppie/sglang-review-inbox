"""SQLite store: PR cards, Slack/GitHub mentions, an event timeline and an
action ledger that makes every external write happen at most once per head."""

import json
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS prs (
    key TEXT PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL,
    title TEXT DEFAULT '', author TEXT DEFAULT '', url TEXT DEFAULT '',
    state TEXT DEFAULT 'OPEN', draft INTEGER DEFAULT 0,
    head_sha TEXT DEFAULT '', base_ref TEXT DEFAULT '',
    additions INTEGER DEFAULT 0, deletions INTEGER DEFAULT 0, changed_files INTEGER DEFAULT 0,
    labels TEXT DEFAULT '[]', mergeable TEXT DEFAULT '', review_decision TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    stage TEXT NOT NULL DEFAULT 'new', stage_reason TEXT DEFAULT '',
    tier TEXT DEFAULT '', priority REAL DEFAULT 0,
    triage TEXT DEFAULT '{}', review TEXT DEFAULT '{}', review_head TEXT DEFAULT '',
    ci TEXT DEFAULT '{}', decision TEXT DEFAULT '{}',
    requesters TEXT DEFAULT '[]',
    first_seen REAL NOT NULL, updated_at REAL NOT NULL, refreshed_at REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mentions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, channel TEXT DEFAULT '',
    author TEXT DEFAULT '', text TEXT DEFAULT '', permalink TEXT DEFAULT '',
    ts REAL NOT NULL, pr_keys TEXT DEFAULT '[]', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pr_key TEXT NOT NULL, ts REAL NOT NULL,
    kind TEXT NOT NULL, message TEXT NOT NULL, data TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_pr ON events(pr_key, id);
CREATE TABLE IF NOT EXISTS actions (
    pr_key TEXT NOT NULL, head TEXT NOT NULL, event TEXT NOT NULL,
    phase TEXT NOT NULL, result TEXT DEFAULT '{}', ts REAL NOT NULL,
    PRIMARY KEY (pr_key, head, event)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS translations (src TEXT PRIMARY KEY, zh TEXT NOT NULL, ts REAL NOT NULL);
"""

JSON_COLUMNS = ("labels", "meta", "triage", "review", "ci", "decision", "requesters")

# Columns added to `mentions` after the first release, so old databases migrate.
# status: pending | replied | reacted | agent (review request handled by the
# pipeline) | resolved (its PRs are merged/closed) | dismissed (you marked it
# handled). conv: channel | dm.
MENTION_COLUMNS = {
    "kind": "TEXT DEFAULT ''", "team_id": "TEXT DEFAULT ''", "channel_id": "TEXT DEFAULT ''",
    "thread_ts": "TEXT DEFAULT ''", "conv": "TEXT DEFAULT 'channel'", "status": "TEXT DEFAULT 'pending'",
    "status_note": "TEXT DEFAULT ''", "checked_at": "REAL DEFAULT 0", "count": "INTEGER DEFAULT 1",
}
MENTION_FIELDS = ("id", "source", "channel", "author", "text", "permalink", "ts", "pr_keys",
                  "created_at", *MENTION_COLUMNS)

# Stages a PR card moves through. Terminal stages are never refreshed again.
ACTIVE_STAGES = ("new", "queued", "reviewing", "ci", "ready", "approved", "needs_author", "error")
TERMINAL_STAGES = ("done", "closed", "dismissed")


def pr_key(repo, number):
    return f"{repo}#{int(number)}"


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(mentions)")}
        for name, ddl in MENTION_COLUMNS.items():
            if name not in columns:
                self.conn.execute(f"ALTER TABLE mentions ADD COLUMN {name} {ddl}")
        self.conn.commit()
        self.path.chmod(0o600)

    # ---- PR cards -------------------------------------------------------
    def _decode(self, row):
        if row is None:
            return None
        pr = dict(row)
        for column in JSON_COLUMNS:
            pr[column] = json.loads(pr[column] or "null")
        pr["draft"] = bool(pr["draft"])
        return pr

    def get_pr(self, key):
        with self.lock:
            row = self.conn.execute("SELECT * FROM prs WHERE key=?", (key,)).fetchone()
        return self._decode(row)

    # ---- Chinese translations of agent-written text ---------------------
    def translations(self, texts):
        texts = list(dict.fromkeys(t for t in texts if t))
        found = {}
        with self.lock:
            for i in range(0, len(texts), 400):
                chunk = texts[i:i + 400]
                found.update(self.conn.execute(
                    f"SELECT src, zh FROM translations WHERE src IN ({','.join('?' * len(chunk))})", chunk).fetchall())
        return found

    def put_translations(self, pairs):
        with self.lock:
            self.conn.executemany("INSERT OR REPLACE INTO translations VALUES (?, ?, ?)",
                                  [(src, zh, time.time()) for src, zh in pairs.items()])
            self.conn.commit()

    def list_prs(self, stages=None):
        query = "SELECT * FROM prs"
        params = ()
        if stages:
            query += f" WHERE stage IN ({','.join('?' * len(stages))})"
            params = tuple(stages)
        query += " ORDER BY priority DESC, updated_at DESC"
        with self.lock:
            rows = self.conn.execute(query, params).fetchall()
        return [self._decode(row) for row in rows]

    def ensure_pr(self, repo, number, requester=None):
        """Create a stub card if unseen; attach who asked. Returns (pr, created)."""
        key = pr_key(repo, number)
        now = time.time()
        with self.lock:
            existing = self.get_pr(key)
            if existing is None:
                self.conn.execute(
                    "INSERT INTO prs (key, repo, number, url, first_seen, updated_at, requesters)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (key, repo, int(number), f"https://github.com/{repo}/pull/{int(number)}",
                     now, now, json.dumps([requester] if requester else [])),
                )
                self.conn.commit()
                self.add_event(key, "discovered", f"discovered (via {(requester or {}).get('source', 'manual')})",
                               requester or {})
                return self.get_pr(key), True
            if requester and requester.get("id") not in {r.get("id") for r in existing["requesters"]}:
                existing["requesters"].append(requester)
                self.update_pr(key, requesters=existing["requesters"])
                if existing["stage"] in TERMINAL_STAGES and existing["state"] == "OPEN":
                    # A fresh ping on a dismissed card brings it back.
                    self.update_pr(key, stage="new", stage_reason="new request")
                self.add_event(key, "pinged", f"{requester.get('author', '?')} asked again", requester)
            return self.get_pr(key), False

    def update_pr(self, key, **fields):
        if not fields:
            return
        fields["updated_at"] = time.time()
        for column in JSON_COLUMNS:
            if column in fields:
                fields[column] = json.dumps(fields[column], ensure_ascii=False)
        if "draft" in fields:
            fields["draft"] = int(bool(fields["draft"]))
        assignments = ", ".join(f"{name}=?" for name in fields)
        with self.lock:
            self.conn.execute(f"UPDATE prs SET {assignments} WHERE key=?", (*fields.values(), key))
            self.conn.commit()

    def set_stage(self, key, stage, reason=""):
        pr = self.get_pr(key)
        if pr is None or (pr["stage"], pr["stage_reason"]) == (stage, reason):
            return
        self.update_pr(key, stage=stage, stage_reason=reason)
        if pr["stage"] != stage:
            self.add_event(key, "stage", f"{pr['stage']} → {stage}" + (f": {reason}" if reason else ""))

    # ---- mentions -------------------------------------------------------
    def _mention_row(self, mention):
        defaults = {"channel": "", "author": "", "permalink": "", "pr_keys": [], "kind": "",
                    "team_id": "", "channel_id": "", "thread_ts": "", "conv": "channel",
                    "status": "pending", "status_note": "", "checked_at": 0, "count": 1}
        row = dict(defaults, **{k: v for k, v in mention.items() if k in MENTION_FIELDS})
        row.update(text=(mention.get("text") or "")[:4000], ts=float(mention["ts"]),
                   pr_keys=json.dumps(row["pr_keys"]), created_at=time.time())
        return tuple(row[name] for name in MENTION_FIELDS)

    def add_mention(self, mention):
        with self.lock:
            cursor = self.conn.execute(
                f"INSERT OR IGNORE INTO mentions ({', '.join(MENTION_FIELDS)})"
                f" VALUES ({', '.join('?' * len(MENTION_FIELDS))})", self._mention_row(mention))
            self.conn.commit()
            return cursor.rowcount == 1

    def upsert_dm(self, item):
        """One row per DM conversation, tracking its latest unanswered message.
        A newer message reopens it even if you had marked the old one handled."""
        with self.lock:
            row = self.conn.execute("SELECT ts, status FROM mentions WHERE id=?", (item["id"],)).fetchone()
            if row is None:
                self.add_mention(item)
            elif float(item["ts"]) > row["ts"]:
                self.conn.execute(
                    "UPDATE mentions SET text=?, ts=?, permalink=?, author=?, count=?, status=?,"
                    " status_note='', checked_at=? WHERE id=?",
                    (item["text"][:4000], float(item["ts"]), item["permalink"], item["author"],
                     item.get("count", 1), item["status"], time.time(), item["id"]))
            elif row["status"] != "dismissed":
                self.conn.execute("UPDATE mentions SET status=?, count=?, checked_at=? WHERE id=?",
                                  (item["status"], item.get("count", 1), time.time(), item["id"]))
            self.conn.commit()

    def set_mention_status(self, mention_id, status, note=""):
        with self.lock:
            self.conn.execute("UPDATE mentions SET status=?, status_note=?, checked_at=? WHERE id=?",
                              (status, note, time.time(), mention_id))
            self.conn.commit()

    def has_mention(self, mention_id):
        with self.lock:
            return self.conn.execute("SELECT 1 FROM mentions WHERE id=?", (mention_id,)).fetchone() is not None

    def get_mention(self, mention_id):
        with self.lock:
            row = self.conn.execute("SELECT * FROM mentions WHERE id=?", (mention_id,)).fetchone()
        return dict(row, pr_keys=json.loads(row["pr_keys"])) if row else None

    def recent_mentions(self, limit=50, statuses=None, since=0):
        query, params = "SELECT * FROM mentions WHERE ts>=?", [since]
        if statuses:
            query += f" AND status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        with self.lock:
            rows = self.conn.execute(query + " ORDER BY ts DESC LIMIT ?", (*params, limit)).fetchall()
        return [dict(row, pr_keys=json.loads(row["pr_keys"])) for row in rows]

    # ---- timeline -------------------------------------------------------
    def add_event(self, key, kind, message, data=None):
        with self.lock:
            self.conn.execute(
                "INSERT INTO events (pr_key, ts, kind, message, data) VALUES (?, ?, ?, ?, ?)",
                (key, time.time(), kind, message, json.dumps(data or {}, ensure_ascii=False)),
            )
            self.conn.commit()

    def count_events(self, kind, since, prefix=""):
        with self.lock:
            row = self.conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind=? AND ts>=? AND substr(message, 1, ?)=?",
                (kind, since, len(prefix), prefix)).fetchone()
        return row[0]

    def last_event_ts(self, kind, prefix=""):
        """When the latest `kind` event whose message starts with `prefix` happened, or 0."""
        with self.lock:
            row = self.conn.execute(
                "SELECT MAX(ts) FROM events WHERE kind=? AND substr(message, 1, ?)=?",
                (kind, len(prefix), prefix)).fetchone()
        return row[0] or 0

    def events(self, key, limit=200):
        with self.lock:
            rows = self.conn.execute(
                "SELECT ts, kind, message, data FROM events WHERE pr_key=? ORDER BY id DESC LIMIT ?",
                (key, limit)).fetchall()
        return [dict(row, data=json.loads(row["data"])) for row in rows]

    # ---- action ledger --------------------------------------------------
    def claim_action(self, key, head, event):
        """Reserve an external write. Returns False when it already ran or is
        in flight; an interrupted 'running' action is left for a human, never
        retried blindly, because the remote write may have happened."""
        with self.lock:
            row = self.conn.execute(
                "SELECT phase FROM actions WHERE pr_key=? AND head=? AND event=?",
                (key, head, event)).fetchone()
            if row is not None:
                return False
            self.conn.execute(
                "INSERT INTO actions VALUES (?, ?, ?, 'running', '{}', ?)",
                (key, head, event, time.time()))
            self.conn.commit()
            return True

    def finish_action(self, key, head, event, ok, result=None):
        with self.lock:
            if ok:
                self.conn.execute(
                    "UPDATE actions SET phase='done', result=?, ts=? WHERE pr_key=? AND head=? AND event=?",
                    (json.dumps(result or {}), time.time(), key, head, event))
            else:
                # Known failure with no remote effect: release so it can be retried.
                self.conn.execute(
                    "DELETE FROM actions WHERE pr_key=? AND head=? AND event=?", (key, head, event))
            self.conn.commit()

    def actions(self, key):
        with self.lock:
            rows = self.conn.execute(
                "SELECT head, event, phase, result, ts FROM actions WHERE pr_key=? ORDER BY ts",
                (key,)).fetchall()
        return [dict(row, result=json.loads(row["result"])) for row in rows]

    # ---- cursors --------------------------------------------------------
    def get_kv(self, key, default=None):
        with self.lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_kv(self, key, value):
        with self.lock:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, json.dumps(value)))
            self.conn.commit()
