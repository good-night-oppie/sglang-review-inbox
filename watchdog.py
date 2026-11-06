"""Keeps the Review Inbox up. Run every 2 minutes by the Windows scheduled task
jj-review-inbox-watchdog (independent of pm2, so it also covers pm2 itself).

- pm2 not answering                                 -> pm2 resurrect (dump has the app)
- app missing from pm2                              -> pm2 start ecosystem.config.js --only app
- app present but not online (stopped / errored after too many crashes) -> pm2 restart
- app online but the dashboard does not answer twice in a row (hung) -> pm2 restart
Writes one line to watchdog.log only when it acts or something is wrong.
"""
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DATA = Path.home() / ".cache" / "jj-review-inbox"
LOG = DATA / "watchdog.log"
STRIKES = DATA / "watchdog-strikes"
PM2 = Path.home() / "AppData" / "Roaming" / "npm" / "pm2.cmd"
APP = "jj-review-inbox"
URL = "http://127.0.0.1:8775/api/state"
HUNG_STRIKES = 2
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg):
    DATA.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 1_000_000:
        LOG.replace(DATA / "watchdog.log.1")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def pm2(*args, timeout=120):
    return subprocess.run([str(PM2), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, creationflags=NO_WINDOW)


def app_status():
    """'online' / 'stopped' / 'errored' / ...; None if the app is not in pm2; raises if pm2 is down."""
    out = pm2("jlist", timeout=60).stdout
    procs = json.loads(out[out.index("["):])
    for p in procs:
        if p.get("name") == APP:
            return p["pm2_env"]["status"]
    return None


def answers():
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(URL, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False


def strikes(n=None):
    if n is None:
        try:
            return int(STRIKES.read_text())
        except (OSError, ValueError):
            return 0
    STRIKES.write_text(str(n))


def main():
    try:
        status = app_status()
    except Exception as exc:
        log(f"pm2 not answering ({exc.__class__.__name__}); pm2 resurrect")
        pm2("resurrect")
        return
    if status is None:
        log(f"{APP} missing from pm2; pm2 start")
        pm2("start", str(Path(__file__).resolve().parent / "ecosystem.config.js"), "--only", APP)
        return
    if status != "online":
        log(f"{APP} is {status}; pm2 restart")
        pm2("restart", APP)
        strikes(0)
        return
    if answers():
        if strikes():
            strikes(0)
        return
    n = strikes() + 1
    if n >= HUNG_STRIKES:
        log(f"dashboard not answering {n} checks in a row; pm2 restart")
        pm2("restart", APP)
        strikes(0)
    else:
        log(f"dashboard not answering (check {n}/{HUNG_STRIKES})")
        strikes(n)


if __name__ == "__main__":
    sys.exit(main())
