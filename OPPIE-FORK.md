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
