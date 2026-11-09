"""Pinned 1.2.1 stdin transport for this Inbox's COMMENT publisher.

No credentials, environment changes or shared-gh fallback. Future App capabilities
are outside this boundary; this candidate exposes only review COMMENT and reads.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess

ENTRY = Path(r"C:\Users\tyb90\AppData\Roaming\CindyGlobal\codex-home\skills\cindy-cindy-plugin-_ns__local__oppie-github%3Aoppie-github-work\scripts\review-request.cjs")
REAL_ENTRY = Path(r"C:\Users\tyb90\AppData\Roaming\CindyGlobal\owners\a1954e134280bba42924\ghost-install-state\skill-snapshots\_ns\local\oppie-github\6a7c78e3-d952-476e-9cc3-8d95e955b87c\skills\oppie-github-work\scripts\review-request.cjs")
SHA256 = "c7681c8930c7c6aab68b91518513b1032cf9c43d73e30ba64d4d48beb6aa044c"
APP_ID = 5687106
INSTALLATION_ID = 180305629
SLUG = "reviewinboxoppiexyz"
ACTOR = SLUG + "[bot]"
REPOS = {"good-night-oppie/MontClip", "good-night-oppie/MontClip-Cindy"}
PERMISSIONS = {"contents": "read", "metadata": "read", "issues": "write", "pull_requests": "write"}
MARKER = re.compile(r"<!-- reviewinbox:[0-9a-f]{32} -->")


class AppError(RuntimeError):
    """unknown=True means a review might exist; never release its ledger claim."""

    def __init__(self, code, unknown=False):
        super().__init__(code)
        self.unknown = unknown


class Client:
    def _entry(self):
        try:
            entry = ENTRY.resolve(strict=True)
            if entry != REAL_ENTRY.resolve(strict=True) or hashlib.sha256(entry.read_bytes()).hexdigest() != SHA256:
                raise AppError("APP_ADAPTER_PIN_MISMATCH")
            return entry
        except OSError:
            raise AppError("APP_ADAPTER_UNAVAILABLE") from None

    def request(self, repo, operation, args):
        """One bounded request; no retry. Only explicit not_executed is retryable."""
        if repo not in REPOS or operation not in {"identity", "get_pull", "list_reviews", "submit_review"}:
            raise AppError("APP_SCOPE_REFUSED")
        write = operation == "submit_review"
        if write and args.get("event") != "COMMENT":
            raise AppError("COMMENT_ONLY")
        data = json.dumps({"repo": repo.split("/")[1], "operation": operation, "args": args}, ensure_ascii=False)
        if len(data.encode("utf-8")) > 262144:
            raise AppError("APP_INPUT_TOO_LARGE")
        entry = self._entry()  # Failure here proves that no process was started.
        try:
            proc = subprocess.run(["node", str(entry)], input=data, capture_output=True,
                                  text=True, encoding="utf-8", errors="strict", timeout=120,
                                  cwd=str(entry.parent), shell=False)
        except (OSError, subprocess.SubprocessError, UnicodeError):
            # An OSError can also occur after process creation / pipe setup.
            raise AppError("APP_TRANSPORT_UNCERTAIN", unknown=write) from None
        try:
            if len(proc.stdout.encode("utf-8")) > 3_000_000:
                raise ValueError()
            response = json.loads(proc.stdout)
            if not isinstance(response, dict):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise AppError("APP_INVALID_RESPONSE", unknown=write) from None
        if response.get("ok") is not True:
            # Do not include stdout/stderr or arbitrary remote text in logs.
            raise AppError("APP_REQUEST_FAILED", unknown=write and response.get("execution") != "not_executed")
        receipt = response.get("receipt") or {}
        expected = {"account": "reviewinbox", "app_slug": SLUG, "installation_id": INSTALLATION_ID,
                    "repo": repo, "operation": operation, "execution": "completed"}
        if proc.returncode != 0 or not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
            raise AppError("APP_RECEIPT_MISMATCH", unknown=write)
        return response.get("result")

    def identity(self, repo):
        result = self.request(repo, "identity", {})
        if not isinstance(result, dict):
            raise AppError("APP_IDENTITY_MISMATCH")
        repositories = result.get("repositories")
        if (result.get("app_id") != APP_ID or result.get("app_slug") != SLUG
                or result.get("owner") != "good-night-oppie" or result.get("permissions") != PERMISSIONS
                or not isinstance(repositories, list) or len(repositories) != 2
                or any(not isinstance(r, dict) for r in repositories)
                or {r.get("full_name") for r in repositories} != REPOS):
            raise AppError("APP_IDENTITY_MISMATCH")

    @staticmethod
    def _validate(repo, number, head, marker):
        if (repo not in REPOS or type(number) is not int or number < 1
                or not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head)
                or not isinstance(marker, str) or not MARKER.fullmatch(marker)):
            raise AppError("APP_INVALID_REVIEW_TARGET")

    @staticmethod
    def _match(item, head, marker):
        if not isinstance(item, dict):
            return False
        user = item.get("user") or {}
        app = item.get("performed_via_github_app")
        return (isinstance(user, dict) and user.get("login") == ACTOR and user.get("type") == "Bot"
                and (app is None or (isinstance(app, dict) and app.get("id") == APP_ID))
                and item.get("commit_id") == head and item.get("state") == "COMMENTED"
                and isinstance(item.get("body"), str) and marker in item["body"].splitlines())

    @staticmethod
    def _result(repo, number, item, marker):
        ident, url = item.get("id"), item.get("html_url")
        if (type(ident) is not int or ident < 1
                or url != f"https://github.com/{repo}/pull/{number}#pullrequestreview-{ident}"):
            raise AppError("APP_REVIEW_RECEIPT_INVALID", unknown=True)
        return {"id": ident, "url": url, "marker": marker, "actor": ACTOR,
                "app_id": APP_ID, "installation_id": INSTALLATION_ID}

    def submit(self, repo, number, head, body, comments, marker):
        self._validate(repo, number, head, marker)
        self.identity(repo)
        snap = self.request(repo, "get_pull", {"number": number})
        if (not isinstance(snap, dict) or (snap.get("head") or {}).get("sha") != head
                or snap.get("state") != "open"):
            raise AppError("APP_STALE_OR_UNREVIEWABLE_HEAD")
        payload = {"number": number, "commit_id": head, "event": "COMMENT",
                   "body": body + "\n\n" + marker, "comments": list(comments)}
        item = self.request(repo, "submit_review", payload)
        if not self._match(item, head, marker):
            raise AppError("APP_REVIEW_RECEIPT_MISMATCH", unknown=True)
        return self._result(repo, number, item, marker)

    def reconcile(self, repo, number, head, marker):
        """Read-only lookup of an unresolved review; absence never permits repost."""
        self._validate(repo, number, head, marker)
        try:
            self.identity(repo)
            snap = self.request(repo, "get_pull", {"number": number})
            if not isinstance(snap, dict) or (snap.get("head") or {}).get("sha") != head or snap.get("state") != "open":
                raise AppError("APP_READBACK_HEAD_MOVED", unknown=True)
            found = []
            for page in range(1, 101):
                items = self.request(repo, "list_reviews", {"number": number, "page": page, "per_page": 100})
                if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                    raise AppError("APP_REVIEW_READBACK_INVALID", unknown=True)
                found.extend(item for item in items if self._match(item, head, marker))
                if len(items) < 100:
                    if len(found) == 1:
                        return self._result(repo, number, found[0], marker)
                    raise AppError("APP_REVIEW_UNRESOLVED", unknown=True)
            raise AppError("APP_READBACK_LIMIT", unknown=True)
        except AppError:
            raise AppError("APP_REVIEW_UNRESOLVED", unknown=True) from None
