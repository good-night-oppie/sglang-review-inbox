#!/usr/bin/env python3
"""Review Inbox: local dashboard + background pipeline.

    python3 server.py --config config.json

Binds to 127.0.0.1 only. Every POST needs the per-process token embedded in
the page, and the Host header must be local, so other web pages cannot
drive actions through your browser. Page tokens never authorize formal approval.
Optional server.tailnet {host, logins}
also admits requests relayed by `tailscale serve` for the listed tailnet users."""

import argparse
import json
import secrets
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from inbox import agent, config as config_module  # noqa: E402
from inbox.db import Store, pr_key  # noqa: E402
from inbox.github import ApprovalDisabled, MergeDisabled, find_pr_refs  # noqa: E402
from inbox.pipeline import Pipeline, finding_comment  # noqa: E402
from inbox.slack import Slack, SlackError  # noqa: E402

TOKEN = ""  # set in main(); kept across restarts so an open page keeps working
RUN_NOW = 3600  # a card you act on may use the agent outside its windows for this long


def load_token(data_dir):
    """The page's anti-CSRF token, stored next to the database (0600) so a
    server restart does not break buttons on a page that is already open."""
    path = Path(data_dir) / "page-token"
    if path.exists() and path.read_text().strip():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token)
    path.chmod(0o600)
    return token
ASSET_TYPES = {".png": "image/png", ".svg": "image/svg+xml"}
SUMMARY_FIELDS = ("key", "repo", "number", "title", "author", "url", "draft", "head_sha",
                  "additions", "deletions", "changed_files", "labels", "mergeable",
                  "review_decision", "stage", "stage_reason", "tier", "priority",
                  "requesters", "first_seen", "updated_at", "review_head")


def card_summary(card, agent_merged=frozenset()):
    summary = {name: card[name] for name in SUMMARY_FIELDS}
    summary["merged_by_agent"] = card["key"] in agent_merged
    summary["merged_by"] = (card["meta"] or {}).get("merged_by", "")
    review = card["review"] or {}
    ci = card["ci"] or {}
    summary["verdict"] = review.get("verdict", "")
    summary["verdict_reason"] = review.get("verdict_reason", "")
    # `risk`: a bug, the only kind that can block (an evidence ask never does)
    summary["findings"] = [{"severity": f["severity"], "verify": f["verify"], "risk": agent.is_risk(f)}
                           for f in review.get("findings", [])]
    summary["ci_status"] = ci.get("status", "")
    summary["ci_counts"] = (ci.get("checks") or {}).get("counts", {})
    summary["ai_signals"] = (card["triage"] or {}).get("ai_signals", [])
    summary["ready_kind"] = card["decision"].get("ready_kind", "")
    summary["merge_ready"] = card["stage"] == "approved" and bool(card["decision"].get("merge_ready"))
    summary["merge_notes"] = card["decision"].get("merge_notes", []) if summary["merge_ready"] else []
    summary["review_stale"] = bool(card["review_head"]) and card["review_head"] != card["head_sha"]
    return summary


class Handler(BaseHTTPRequestHandler):
    server_version = "ReviewInbox/0.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, payload, content_type="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if host in ("127.0.0.1", "localhost"):
            return True
        # Phone access through `tailscale serve` (tailnet only, never Funnel): the
        # request still arrives on 127.0.0.1, with the machine's ts.net name as Host
        # and the tailnet user that Tailscale itself vouches for. Tagged devices and
        # anyone not listed get nothing.
        tailnet = getattr(self.server, "tailnet", None) or {}
        login = (self.headers.get("Tailscale-User-Login") or "").lower()
        return bool(tailnet) and host == tailnet["host"] and login in tailnet["logins"]

    def do_GET(self):
        if not self._host_ok():
            return self._send(HTTPStatus.FORBIDDEN, {"error": "bad host"})
        url = urlparse(self.path)
        app = self.server.app
        if url.path == "/":
            page = (ROOT / "web" / "index.html").read_text().replace("__INBOX_TOKEN__", TOKEN)
            return self._send(HTTPStatus.OK, page.encode(), "text/html; charset=utf-8")
        if url.path.startswith("/assets/"):
            name = url.path[len("/assets/"):]
            asset = ROOT / "web" / "assets" / name
            if "/" in name or not asset.is_file() or asset.suffix not in ASSET_TYPES:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return self._send(HTTPStatus.OK, asset.read_bytes(), ASSET_TYPES[asset.suffix])
        if url.path == "/api/state":
            store, pipeline = app["store"], app["pipeline"]
            agent_merged = store.agent_merged_keys()
            cards = [card_summary(card, agent_merged) for card in store.list_prs()]
            teams = {c.team_id for c in pipeline._slack if getattr(c, "team_id", None)}
            return self._send(HTTPStatus.OK, {
                "now": time.time(),
                "mode": pipeline.config["mode"],
                "runner": pipeline.config["agent"]["runner"],
                "login": pipeline.config["github_login"],
                "status": pipeline.status,
                "inflight": dict(pipeline.inflight),
                "agent": pipeline.agent_status(),  # windows, today's tokens, the budget
                "slack_workspaces": [getattr(c, "team", "") or "?" for c in pipeline._slack],
                "review_max_age_days": pipeline.config["sources"]["slack"]["review_max_age_days"],
                "policy": {k: v for k, v in pipeline.policy.items() if k.startswith(("auto_", "slack_"))},
                "cards": cards,
                # Chinese for the agent text that shows up in list rows (stage reasons)
                "zh": store.translations([c["verdict_reason"] for c in cards]),
                # only workspaces still connected; an unbound one's messages drop out
                "mentions": [m for m in store.recent_mentions(400, since=time.time() - 14 * 86400)
                             if not teams or m["source"] != "slack" or m["team_id"] in teams],
            })
        if url.path == "/api/pr":
            key = parse_qs(url.query).get("key", [""])[0]
            card = app["store"].get_pr(key)
            if card is None:
                return self._send(HTTPStatus.NOT_FOUND, {"error": "unknown PR"})
            events = app["store"].events(key)
            if (card["review"] or {}).get("findings"):  # each comment exactly as it is (or would be) posted
                card["review"] = dict(card["review"], findings=[
                    dict(f, comment=app["pipeline"].sign(finding_comment(f))) for f in card["review"]["findings"]])
            texts = app["pipeline"].agent_texts(card) + [
                e["message"].removeprefix("CI triage: ") for e in events if e["message"].startswith("CI triage: ")]
            # Chinese is translated on demand, when you open a card in the Chinese view.
            pending = parse_qs(url.query).get("lang", [""])[0] == "zh" and app["pipeline"].request_translation(key)
            merged_by_agent = any(e["kind"] == "merge" and e["message"].startswith("agent merged") for e in events)
            return self._send(HTTPStatus.OK, dict(card, events=events, actions=app["store"].actions(key),
                                                  zh=app["store"].translations(texts), zh_pending=pending,
                                                  merged_by_agent=merged_by_agent,
                                                  merged_by=(card["meta"] or {}).get("merged_by", "")))
        return self._send(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Inbox-Token") != TOKEN:
            return self._send(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            result = self.route(urlparse(self.path).path, body)
        except (ApprovalDisabled, MergeDisabled) as exc:
            return self._send(HTTPStatus.FORBIDDEN, {"error": str(exc)})
        except (ValueError, KeyError) as exc:
            self.log_failure(body, exc)
            return self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # surface GitHub/agent errors to the page
            self.log_failure(body, exc)
            return self._send(HTTPStatus.BAD_GATEWAY, {"error": str(exc)[:500]})
        return self._send(HTTPStatus.OK, result or {"ok": True})

    def log_failure(self, body, exc):
        """A failed click is also kept in the PR's timeline, not only in a toast."""
        key = body.get("key") if isinstance(body, dict) else None
        if key and self.server.app["store"].get_pr(key):
            self.server.app["store"].add_event(key, "error", f"your {body.get('action', 'action')} failed: {str(exc)[:300]}")

    def route(self, path, body):
        store, pipeline = self.server.app["store"], self.server.app["pipeline"]
        if path == "/api/poll":
            pipeline.kick(reset_sources=True)
            return None
        if path == "/api/mode":
            if body["mode"] not in ("shadow", "live"):
                raise ValueError("mode must be shadow or live")
            pipeline.config["mode"] = body["mode"]
            store.set_kv("mode", body["mode"])
            return None
        if path == "/api/add":
            refs = find_pr_refs(body["url"], set(pipeline.config["repos"]))
            if not refs:
                raise ValueError("no PR link from an allowed repo found")
            for repo, number in refs:
                store.ensure_pr(repo, number, requester={
                    "id": f"manual:{repo}#{number}", "source": "manual",  # adding again is not a new ask
                    "author": body.get("who") or "added by hand", "text": body["url"][:300], "ts": time.time()})
                card = store.get_pr(pr_key(repo, number))
                store.update_pr(card["key"], decision=dict(card["decision"], run_now_until=time.time() + RUN_NOW))
            pipeline.kick()
            return {"added": [pr_key(*ref) for ref in refs]}
        if path == "/api/action":
            return self.action(store, pipeline, body)
        if path == "/api/agent":  # the daily token cap and runs you start by hand
            if body["action"] == "budget":
                tokens = int(body["tokens"])
                if not 0 <= tokens <= 1_000_000_000:
                    raise ValueError("the daily limit must be between 0 and 1000M tokens")
                pipeline.set_token_budget(tokens)
            elif body["action"] == "run_now":
                pipeline.run_now()
            elif body["action"] == "stop_run":
                pipeline.stop_run()
            else:
                raise ValueError("action must be budget, run_now or stop_run")
            return pipeline.agent_status()
        if path == "/api/slack-token":
            return self.save_slack_token(pipeline, body.get("token", "").strip())
        if path == "/api/slack-item":
            item = store.get_mention(body["id"])
            if item is None:
                raise ValueError("unknown message")
            if body["action"] == "done":
                store.set_mention_status(item["id"], "dismissed", "marked handled")
            elif body["action"] == "reopen":
                store.set_mention_status(item["id"], "pending")
            else:
                raise ValueError("action must be done or reopen")
            return None
        raise ValueError("unknown endpoint")

    def save_slack_token(self, pipeline, token):
        """You paste a user token in the page; it is checked with auth.test and
        appended to the 0600 token file. It is never logged or returned."""
        if not token.startswith("xoxp-"):
            raise ValueError("needs a User OAuth Token (xoxp-...), not a bot token")
        client = Slack(token)
        try:
            client.identity()
            client.call("search.messages", {"query": "review", "count": 1})
        except SlackError as exc:
            raise ValueError(f"Slack check failed: {exc} (needs the search:read scope)") from exc
        path = Path(pipeline.config["sources"]["slack"]["token_file"])
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        existing = path.read_text().splitlines() if path.exists() else []
        kept = [line for line in existing if line.strip()]
        if token not in kept:
            kept.append(token)
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text("\n".join(kept) + "\n")
        pipeline.kick(reset_sources=True)
        return {"team": client.team, "team_id": client.team_id}

    def action(self, store, pipeline, body):
        if body.get("action") == "approve":
            # CSRF token possession is not an attested reviewer identity. No
            # request fields, page mode or role claim can opt into this action.
            raise ApprovalDisabled("FORMAL_APPROVAL_DISABLED")
        if body.get("action") == "merge":
            raise MergeDisabled("FORMAL_MERGE_DISABLED")
        key, action = body["key"], body["action"]
        card = store.get_pr(key)
        if card is None:
            raise ValueError("unknown PR")
        decision = dict(card["decision"], refresh=True)
        if action == "publish_findings":
            result = pipeline.publish_findings(key, by="human",
                draft_request=pipeline.record_draft_request(key, action))
        elif action == "request_changes":
            # Already posted (by you or the agent) is fine: this click is about whose turn it is.
            result = pipeline.findings_review(key, card["head_sha"]) or pipeline.publish_findings(key, by="human",
                draft_request=pipeline.record_draft_request(key, action))
            decision["waiting_author_head"] = card["head_sha"]  # the pipeline keeps it there until a new push
            store.set_stage(key, "needs_author", "you confirmed the findings and sent them to the author")
        elif action == "run_suggestion":
            suggestion = next((s for s in (card["ci"] or {}).get("suggestions", [])
                               if s["event"] == body["event"]), None)
            if suggestion is None:
                raise ValueError("this suggestion is stale, refresh and retry")
            if not pipeline.run_command(key, card["head_sha"], suggestion, by="human"):
                raise ValueError("this command was already sent for this head")
            result = None
        elif action == "force_review":
            decision["force_review_head"] = card["head_sha"]
            decision["run_now_until"] = time.time() + RUN_NOW
            store.add_event(key, "decision", "you asked to review despite the gates")
            result = None
        elif action == "rereview":  # a full review, now, even outside the agent windows
            store.update_pr(key, review_head="")
            decision.update(error_count=0, waiting_author_head="", full_review=True,
                            run_now_until=time.time() + RUN_NOW)
            store.add_event(key, "decision", "you asked for a re-review")
            result = None
        elif action in ("defer", "dismiss"):
            store.set_stage(key, "dismissed", "deferred" if action == "defer" else "won't handle")
            result = None
        elif action == "restore":
            store.set_stage(key, "new", "you restored this card")
            decision.update(waiting_author_head="", run_now_until=time.time() + RUN_NOW)
            result = None
        else:
            raise ValueError(f"unknown action {action}")
        store.update_pr(key, decision=dict(store.get_pr(key)["decision"], **{
            k: v for k, v in decision.items()
            if k in ("refresh", "force_review_head", "error_count", "waiting_author_head", "full_review",
                     "run_now_until")}))
        pipeline.kick()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=str(ROOT / "config.json"))
    parser.add_argument("--no-pipeline", action="store_true", help="serve the dashboard only")
    args = parser.parse_args()
    config = config_module.load(args.config)
    global TOKEN
    TOKEN = load_token(config["data_dir"])
    store = Store(Path(config["data_dir"]) / "inbox.sqlite3")
    pipeline = Pipeline(config, store)
    pipeline.backfill_usage(Path(config["data_dir"]) / "runs")
    if not args.no_pipeline:
        pipeline.start()
    server = ThreadingHTTPServer((config["server"]["host"], config["server"]["port"]), Handler)
    server.app = {"store": store, "pipeline": pipeline}
    tailnet = config["server"].get("tailnet") or {}
    server.tailnet = {"host": tailnet["host"].lower().rstrip("."),
                      "logins": {x.lower() for x in tailnet["logins"]}} if tailnet.get("logins") else None
    print(f"Review Inbox on http://127.0.0.1:{config['server']['port']}  mode={config['mode']}"
          f"  runner={config['agent']['runner']}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pipeline.stopping.set()


if __name__ == "__main__":
    main()
