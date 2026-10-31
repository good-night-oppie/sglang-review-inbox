"""Agent fixes pushed to a PR branch, so a PR does not wait on its author for
things the agent can do: merge main and resolve conflicts, fix verified review
findings or CI failures caused by the change.

Git is driven here; the agent only edits files. Commits carry your GitHub
noreply identity (so they count as attributed), pushes are fast-forward only,
never forced, and only to branches the author lets maintainers edit."""

import re
import subprocess
from pathlib import Path

MARKER = re.compile(r"^(<<<<<<<|>>>>>>>)( |$)", re.M)


class FixError(RuntimeError):
    pass


def commit_message(summary, limit=72):
    """Subject of at most `limit` characters, cut at a word and never on a
    dangling connective; when cut, the full line goes in the body."""
    summary = " ".join(summary.split()).rstrip(".") or "Address review findings"
    if len(summary) <= limit:
        return summary
    words = summary[:limit + 1].split(" ")[:-1]
    while words and words[-1].lower().rstrip(",;:") in ("and", "or", "to", "the", "a", "in", "of", "with", "for"):
        words.pop()
    return " ".join(words).rstrip(",;:") + "\n\n" + summary + "."


class Checkout:
    """A detached checkout of the PR head that the agent edits in place."""

    def __init__(self, worktree, name, email):
        self.dir = Path(worktree)
        self.identity = ["-c", f"user.name={name}", "-c", f"user.email={email}"]

    def git(self, *args, check=True, timeout=900):
        proc = subprocess.run(["git", "-C", str(self.dir), *self.identity, *args],
                              capture_output=True, text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise FixError(f"git {args[0]}: {(proc.stderr or proc.stdout).strip()[-300:]}")
        return proc

    def head(self):
        return self.git("rev-parse", "HEAD").stdout.strip()

    def merge(self, ref):
        """Merge `ref`; returns the conflicted files ([] = merged and committed)."""
        proc = self.git("merge", "--no-edit", "--no-ff", ref, check=False)
        conflicted = self.git("diff", "--name-only", "--diff-filter=U").stdout.split()
        if proc.returncode != 0 and not conflicted:
            raise FixError(f"git merge: {(proc.stderr or proc.stdout).strip()[-300:]}")
        return conflicted

    def unresolved(self, files):
        return [f for f in files if (self.dir / f).exists()
                and MARKER.search((self.dir / f).read_text(errors="replace"))]

    def finish_merge(self, files):
        self.git("add", "--", *files)
        self.git("commit", "--no-edit")

    def abort_merge(self):
        self.git("merge", "--abort", check=False)

    def changed_files(self):
        out = self.git("status", "--porcelain", "--untracked-files=all").stdout
        return [line[3:].split(" -> ")[-1] for line in out.splitlines() if line.strip()]

    def tidy(self, files):
        """The repo's own formatters (pre-commit), best effort: a hook that
        cannot install offline must not block the fix."""
        if not (self.dir / ".pre-commit-config.yaml").exists():
            return
        existing = [f for f in files if (self.dir / f).exists()]
        for _ in range(2):  # formatters may need a second pass to settle
            try:
                subprocess.run(["pre-commit", "run", "--files", *existing], cwd=self.dir,
                               capture_output=True, text=True, timeout=900)
            except (OSError, subprocess.TimeoutExpired):
                return

    def compile_check(self, files):
        for f in files:
            if f.endswith(".py") and (self.dir / f).exists():
                proc = subprocess.run(["python3", "-m", "py_compile", f], cwd=self.dir,
                                      capture_output=True, text=True, timeout=120)
                if proc.returncode != 0:
                    raise FixError(f"{f} does not compile: {proc.stderr.strip()[-300:]}")

    def commit(self, message, files):
        self.git("add", "-A", "--", *files)
        self.git("commit", "-m", message)

    def push(self, url, branch):
        """Fast-forward only: if the author pushed meanwhile this fails and
        nothing of theirs is overwritten."""
        self.git("-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
                 "push", url, f"HEAD:refs/heads/{branch}", timeout=600)
