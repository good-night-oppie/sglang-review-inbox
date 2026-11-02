---
name: jj-agent-review
description: "Review jj-agent (哔站切片助手) pull requests, diffs or local changes the way its maintainers do: local-only 127.0.0.1 service, no path escapes, ffmpeg argv without a shell, never overwrite files, zero network by default, no secrets or viewer names in logs, clean-room (no reference/capcut-investigation material), 40 ms A/V sync check for export changes, licence + SHA-256 for new dependencies. Use when asked to review, pre-review or re-review a jj-agent PR, patch or branch; verdicts are 通过 / 要改 / 需要讨论."
---

# jj-agent Review

Use this skill when reviewing a pull request, patch, branch or working tree of
`good-night-oppie/jj-agent` (哔站切片助手: a local Windows clip tool for
streamers who are not comfortable with computers). It mirrors the structure of
the SGLang `sglang-humanize-review` skill but carries jj-agent's own maintainer
standards; nothing from SGLang applies here.

## Required reading

Before any review, read [references/maintainer-standards.md](references/maintainer-standards.md)
in full. It is the current review bar of the jj-agent maintainers (Deja as
release owner, QQ as chief code reviewer, as of 2026-10-31): the hard rules
("red lines"), the review order, what blocks and what does not, the evidence a
PR must carry, and how the verdict is written. Where this file and any older
PR comment disagree, the standards file wins.

The repository's own documents are the source of those standards. Read the
ones the diff touches:

- `reference/ADR-001-desktop-shell.md`: the window is a local page served on
  127.0.0.1 by a stdlib server; Host/Origin checks; lifecycle rules.
- `reference/ADR-002-speech-to-text.md`: speech recognition must stay offline
  and must not load PyAV (LGPL-only shipping).
- `reference/ADR-003-release-2026-10-30.md`: ubuntu-24.04 / FFmpeg 6.1 is the
  required CI gate; the release package is an allow-list; the clean-room rule.
- `VERIFICATION.md`: what has actually been verified, with numbers; what is
  known to be unverified.
- `LICENSES.md` and `第三方组件许可.txt`: the third-party component record.

Never open, quote or summarise anything under `reference/capcut-investigation/`.
If the diff adds, moves or references material from that directory, that is
itself a blocking finding (see standards §1.9).

## Review workflow

1. **Get the real diff.** `gh pr diff <n>` or `git diff <merge-base>..<head>`;
   for a PR, check out the PR head and read callers and callees there, not in a
   local checkout at another commit. Note which of the high-risk files are
   touched: `scripts/clip.py`, `scripts/project.py`, `scripts/app_server.py`,
   `scripts/run_app.py`, `scripts/build_package.py`, `scripts/package_check.py`,
   `scripts/ai_editor.py`, `scripts/recorder.py`, `app/app.js`.
2. **Read the standards** (`references/maintainer-standards.md`) and the ADRs
   the diff touches.
3. **Comprehension pass, before any finding.** 2–6 bullets: which part of the
   product changes, the mechanism, the one or two lines that carry the
   behaviour change, the entry points. For export, preview, server or packaging
   changes add a short `flowchart TD` Mermaid diagram of the runtime path with
   changed nodes marked (`:::changed`, legend node, `classDef changed
   stroke-dasharray:5 5,stroke-width:2px;`), every label in double quotes.
4. **Check the red lines first** (standards §1), in order. Each is a concrete
   search in the diff and the touched files, not a feeling.
5. **Then correctness** on realistic streamer paths: wrong cut points, A/V
   drift, a file overwritten, a half-written export left behind, a Chinese
   error message that lies, an undo step that does not restore state.
6. **Then tests** (standards §3): does each new test reach the changed branch
   and would it go red if the change were reverted? Are the two commands
   `python3 -B -I -m unittest discover -s tests -v` and `ruff check .` run and
   reported with numbers?
7. **Then evidence in the description** (standards §4): test numbers on the
   current head, A/V sync readings for export changes, licence + SHA-256 for
   new components, what was *not* run.
8. **Write the verdict** (standards §5): 通过 / 要改 / 需要讨论, findings as
   `文件:行号`, `具体输入 → 错误结果`, `建议改法`, split into 「确认的 bug」
   and 「可能有问题」, closing with 看到的事实 / 推断 / 还不知道的.

## Blocking rule

Only a concrete bug blocks: a specific input that produces a wrong result, a
crash, data loss, a file overwritten, a network call where none is allowed, a
path outside the chosen folder, a secret or nickname in a log, clean-room
contamination, or a licence record missing for a shipped component. A request
for more tests, more benchmarks, another platform run or a real-streamer trial
is a non-blocking note. The same evidence is never asked for twice; if an
earlier comment already asked, say so and move on.

## Incremental re-review

When the PR was already reviewed at an earlier head:

- Diff only `<reviewed-head>..<new-head>` restricted to the PR's files. If the
  author merged main in between, use `git log` to tell main's commits apart and
  do not review them.
- For each earlier finding, say fixed / still there (with the new line) /
  no longer applies. Do not re-review unchanged parts and do not add new
  evidence asks.
- A finding raised earlier that the new head still has keeps its severity.
- New findings only in the changed lines; verdict as in a full review.

## Output contract

For a full review, in this order:

1. Verdict line: `## 审查：通过` / `## 审查：要改` / `## 审查：需要讨论`, with a
   half-sentence reason.
2. 我跑了什么 / 没跑什么 (commands, counts, platform; "没跑" is a valid entry).
3. Comprehension block (bullets, plus the Mermaid flow when required).
4. 确认的 bug (each: `文件:行号`; `输入 → 错误结果`; `建议改法`), ordered by
   severity.
5. 可能有问题 (same shape, marked as inference, non-blocking).
6. 非阻塞备注 (tests, evidence, follow-ups).
7. 看到的事实 / 推断 / 还不知道的.

Write for the maintainers in Chinese; keep code identifiers, paths and command
lines unchanged. Anything addressed to the streamer (error text, UI copy) is
judged by the plain-language rule in standards §1.11. Do not add greetings,
praise, or restatements of the diff. Say what you did not validate.
