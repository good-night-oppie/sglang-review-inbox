# Oppie source-derived fork

This is a private, Git-history-preserving maintenance fork, not a GitHub-native fork. GitHub reports fork=false. The recorded upstream https://github.com/BBuf/sglang-review-inbox returned HTTP 404 during verification on 2026-11-09; no accessible replacement was found. Do not describe GitHub parent linkage as verified.

- Recorded upstream/local vendor base: 147b8cd912c3fc8695d176dfd74b7360cae5e90d.
- Imported local source: jj-agent-local at 25fc4d9d9dca25a5756ea00f0bb3a237fc7e5c28, 11 local commits after that base.
- Remote maintenance repository: https://github.com/good-night-oppie/sglang-review-inbox.
- Original license and authorship are preserved. Only committed Git objects were cloned. Runtime config.json, untracked backups, databases and credentials are excluded.
- All 21 reachable commits / 130 unique blobs received a bounded secret-signature screen with no credential pattern hits. This is not an exhaustive proof. ecosystem.config.js contains historical machine paths; it is not a portable deployment recipe.
- ReviewInboxOppieXYZ COMMENT adapter is under separate development and is NOT present in this baseline.

This source archive does not deploy, restart or alter the live Inbox. GG maintains source/submodule updates; ReviewInbox manages the instance and its isolated adapter candidate; QQ reviews changes before release. Parent montclip-rsi pins an exact commit and does not execute this submodule in the patrol loop.

Validation: python -m unittest discover -s tests (offline). Current-head test evidence is recorded in the parent delivery. Future updates must preserve unknown-write ledger records and reconcile remote results before retrying; never fall back to a shared GitHub identity when App authentication fails.

## COMMENT candidate source provenance

The review/app-comment branch imports only inbox/, tests/ and docs/ changes from original candidate 767f3f02412a302e08eb5e3b6f2471d52b6673b5 against25fc4d9d9dca25a5756ea00f0bb3a237fc7e5c28. Source/test trees are checked byte-for-byte. Local .execution_logs and .qa_logs are not published; this does not modify the original candidate or its ongoing work.

Original reported validation:20 focused passed; full190=172passed/17existing skips/1baseline SkillTest failure. Publication checkout independently reran20focused successfully. Ruff was unavailable in original environment. Real verification was identity-only; no GitHub comments were sent.

Deployment holds: fixed installed1.2.1 Windows Skill path/hash is machine-specific; plugin Draft COMMENT fix is separate oppie-github#1, not installed; managed service integration is separate oppie-github#2 and preview-only. Formal QQ approval remains pending. No new approval-gate changes from the original working tree are included.

## Combined candidate update

Original candidate 2b7f039082a1949ff0728ce3f51369deaaea5be1 supersedes767f3f0 for review. Source/test/docs/config.example.json/server.py/web trees match that exact candidate; execution/QA logs remain excluded. User-specific installation paths are now explicit private runtime settings, not source defaults.

This update blocks page-token approve AND merge before external requests, labels human-confirmed COMMENT findings separately, and derives optional Draft provenance from persisted service-owned sources only. It does not enable future approval automation. The existing installed1.2.1 hash remains pinned, so actual Draft posting still fails closed pending the reviewed plugin update.

Independent publication validation:32 App/source tests and6 page gate tests passed. Original full result208=190pass/17skip/1unchanged baseline failure; lint unavailable. No deployment or real comment writes. QQ approval is still pending and the plugin parent pins remain unchanged.
