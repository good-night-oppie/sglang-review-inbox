"""Keep the review skill at the latest commit of its git branch (or use a
local directory), and check that an agent run actually used it.

Overlay for jj-agent: identical to upstream sglang-review-inbox except that
the corpus-sweep steps are required only when the skill ships the sweep
script / supplementary corpus. jj-agent-review has no review corpus, so a run
must read SKILL.md and references/maintainer-standards.md, nothing more."""

import re
import subprocess
import threading
import time
from pathlib import Path

# Reference files that, when the skill ships them, every review must read.
REQUIRED_REFERENCES = ("references/maintainer-standards.md",)
SWEEP_SCRIPT = "scripts/summarize_sglang_review_corpus.py"
SUPPLEMENT_CORPUS = "references/bbuf-recent-review-corpus.jsonl.gz"
EXEC_LINE = re.compile(r"^/bin/(?:zsh|bash|sh) -lc (.+)$", re.M)


class SkillSyncError(RuntimeError):
    pass


def _git(cwd, *args, timeout=900):
    proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise SkillSyncError(f"git {' '.join(args[:2])}: {proc.stderr.strip()[-300:]}")
    return proc.stdout.strip()


class SkillSync:
    """A private clone that tracks `branch`; refreshed at most every `sync_seconds`."""

    def __init__(self, config):
        spec = config["agent"]["review_skill"]
        self.url, self.branch, self.subdir = spec["repo"], spec["branch"], spec["subdir"]
        self.seed = Path(spec.get("seed", "")).expanduser() if spec.get("seed") else None
        self.sync_seconds, self.max_stale = spec["sync_seconds"], spec["max_stale_seconds"]
        self.clone = Path(config["data_dir"]) / "skills-repo"
        self.lock = threading.Lock()
        self.synced_at = 0.0
        self.commit = ""

    def ensure(self):
        """Return (skill_dir, commit) at the latest branch tip we could reach."""
        with self.lock:
            if time.time() - self.synced_at >= self.sync_seconds:
                try:
                    self._sync()
                    self.synced_at = time.time()
                except (SkillSyncError, subprocess.TimeoutExpired) as exc:
                    if not self.commit or time.time() - self.synced_at > self.max_stale:
                        raise SkillSyncError(f"cannot sync the review skill: {exc}") from exc
            skill_dir = self.clone / self.subdir
            if not (skill_dir / "SKILL.md").exists():
                raise SkillSyncError(f"{skill_dir}/SKILL.md is missing on {self.branch}")
            return skill_dir, self.commit

    def _sync(self):
        if not (self.clone / ".git").exists():
            self.clone.parent.mkdir(parents=True, exist_ok=True)
            cmd = ["git", "clone", "--branch", self.branch]
            if self.seed and (self.seed / ".git").exists():
                cmd += ["--reference-if-able", str(self.seed), "--dissociate"]
            proc = subprocess.run([*cmd, self.url, str(self.clone)], capture_output=True, text=True, timeout=1800)
            if proc.returncode != 0:
                raise SkillSyncError(f"git clone: {proc.stderr.strip()[-300:]}")
        _git(self.clone, "fetch", "--quiet", "origin", self.branch)
        _git(self.clone, "reset", "--quiet", "--hard", f"origin/{self.branch}")
        self.commit = _git(self.clone, "rev-parse", "HEAD")


def missing_skill_steps(transcript, skill_dir):
    """Steps of the skill a review run skipped, judged from the commands the
    agent executed (codex logs each as `/bin/zsh -lc ...`)."""
    commands = "\n".join(EXEC_LINE.findall(transcript))
    # Windows: codex prints no `/bin/zsh -lc` lines and uses backslash paths; judge from the whole
    # transcript with separators normalised, so a review that did read the skill is not rejected.
    if not commands.strip():
        commands = transcript
    commands = commands.replace("\\\\", "/").replace("\\", "/")
    skill_dir = str(skill_dir).replace("\\", "/")
    missing = []
    if f"{skill_dir}/SKILL.md" not in commands:
        missing.append("read SKILL.md")
    if (Path(skill_dir) / SWEEP_SCRIPT).exists() and SWEEP_SCRIPT.split("/")[-1] not in commands:
        missing.append("run the corpus sweep")
    for ref in REQUIRED_REFERENCES:
        if (Path(skill_dir) / ref).exists() and ref.split("/")[-1] not in commands:
            missing.append(f"read {ref}")
    if (Path(skill_dir) / SUPPLEMENT_CORPUS).exists() and SUPPLEMENT_CORPUS.split("/")[-1] not in commands:
        missing.append("sweep the maintainer's recent review corpus")
    return missing
