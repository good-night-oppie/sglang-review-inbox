"""Daily housekeeping for the Review Inbox data directory (run by pm2 cron).

- runs/: delete run folders older than KEEP_DAYS, then the oldest until the total
  is under MAX_RUNS_MB. A folder modified in the last hour is never touched
  (it may belong to a review that is still running).
- server.log: when over MAX_LOG_MB, copy to server.log.1 (keeping LOG_KEEP old
  copies) and truncate in place; pm2 keeps appending to the same file.
- inbox.sqlite3: WAL checkpoint so the -wal file does not keep growing.
- memory: if the Inbox python process uses more than MAX_MEM_MB, ask pm2 to
  restart it (pm2 itself only sees the conhost wrapper, so its own limit can't).
Only paths inside DATA are touched. Prints one summary line.
"""
import shutil
import subprocess
import sqlite3
import sys
import time
from pathlib import Path

DATA = Path.home() / ".cache" / "jj-review-inbox"
KEEP_DAYS = 14
MAX_RUNS_MB = 500
MAX_LOG_MB = 20
LOG_KEEP = 3
BUSY_SECONDS = 3600
MAX_MEM_MB = 1024
PM2 = Path.home() / "AppData" / "Roaming" / "npm" / "pm2.cmd"


def size(path):
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def prune_runs(now):
    runs = DATA / "runs"
    if not runs.is_dir():
        return 0, 0
    dirs = sorted((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)
    sizes = {d: size(d) for d in dirs}
    total = sum(sizes.values())
    removed = 0
    for d in dirs:
        age = now - d.stat().st_mtime
        if age < BUSY_SECONDS:
            break
        if age > KEEP_DAYS * 86400 or total > MAX_RUNS_MB * 2**20:
            shutil.rmtree(d, ignore_errors=True)
            if not d.exists():
                total -= sizes[d]
                removed += 1
    return removed, total


def rotate_log():
    log = DATA / "server.log"
    if not log.is_file() or log.stat().st_size < MAX_LOG_MB * 2**20:
        return False
    for i in range(LOG_KEEP - 1, 0, -1):
        older = DATA / f"server.log.{i}"
        if older.exists():
            older.replace(DATA / f"server.log.{i + 1}")
    shutil.copyfile(log, DATA / "server.log.1")
    with open(log, "r+b") as f:
        f.truncate(0)
    return True


def checkpoint():
    db = DATA / "inbox.sqlite3"
    if not db.is_file():
        return
    conn = sqlite3.connect(db, timeout=30)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def inbox_memory_mb():
    """Working set of the Inbox python process (Windows); None if not found."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
          "Where-Object { $_.CommandLine -like '*server.py --config config.json*' } | "
          "ForEach-Object { $_.WorkingSetSize }")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    nums = [int(x) for x in out.split() if x.isdigit()]
    return max(nums) / 2**20 if nums else None


def main():
    now = time.time()
    removed, total = prune_runs(now)
    rotated = rotate_log()
    try:
        checkpoint()
        ck = "ok"
    except sqlite3.Error as exc:
        ck = f"skipped ({exc.__class__.__name__})"
    mem = inbox_memory_mb() if sys.platform == "win32" else None
    restarted = False
    if mem is not None and mem > MAX_MEM_MB:
        subprocess.run([str(PM2), "restart", "jj-review-inbox"], capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        restarted = True
    mem_txt = "n/a" if mem is None else f"{mem:.0f}MB"
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} housekeep: runs removed={removed} "
          f"runs total={total / 2**20:.0f}MB log rotated={rotated} wal checkpoint={ck} "
          f"inbox memory={mem_txt} restarted={restarted}")


if __name__ == "__main__":
    sys.exit(main())
