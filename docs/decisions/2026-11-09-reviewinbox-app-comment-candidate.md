# ReviewInbox App COMMENT transport candidate

Status: candidate only; no deployment, restart, new schedule or GitHub review posted.

Current behavior and runtime configuration are specified in `2026-11-09-qq-followup-and-draft-provenance.md`; that follow-up supersedes the historical human-label, page-merge and embedded-path descriptions below. Current QA is `.qa_logs/2026-11-09T22_30_00_combined_validation_request.md`.

Supplement: the owner subsequently requested manual approval-channel closure. Current candidate disables page/default-human approval and reserves a closed classifier stub; see `.qa_logs/2026-11-09T21_41_00_approval_validation_request.md`. Statements below describing unchanged human approval are historical to 767f3f0, superseded for that channel only. Merge, COMMENT and external formal-review recognition retain their existing semantics.

The owner selected Cindy workflow for this candidate, replacing the missing legacy rule/sender requirement only within this scope. See the Template E and applicability decision in `.execution_logs/2026-11-09T21_20_00_executor_applicability.md`.

## Behavior

The existing `github.submit_agent_review` seam now uses `review_app.Client`, never shared gh, to publish COMMENT. It labels new advisory output as ReviewInboxOppieXYZ and pins the full reviewed head. Both automatic findings and human-confirmed Agent findings use this transport. Human filtering and request_changes button handling remain in their original Pipeline/server paths. Human APPROVE and merge functions are unchanged. No historical action or comment is rewritten.

The Python transport pins App 5687106 / installation 180305629 / slug reviewinboxoppiexyz / two good-night-oppie repositories. It validates identity before submitting, then checks the live open PR head; the plugin also independently checks identity and head. The verified 1.2.1 Skill projection is resolved to real snapshot 6a7c78e3-d952-476e-9cc3-8d95e955b87c and SHA256 c7681c8930c7c6aab68b91518513b1032cf9c43d73e30ba64d4d48beb6aa044c. A changed target/hash blocks requests. No token or private key enters Python output, command arguments or environment changes. Only the JSON request is passed over stdin; no shell or automatic retry.

## Ambiguous outcomes

A random unique marker is stored atomically with the existing action claim before submission. A valid App review receipt must match actor, full head, COMMENTED state, marker and PR review URL. Explicit no-effect failure can release a running claim. Timeout, disconnect, malformed or conflicting success response leave `unresolved`, preserving its marker. Existing interrupted `running` claims are also read-only recovered; a legacy running claim without marker is held for manual investigation.

Repeated triggers perform only App list_reviews readback, with pagination, validating live head and actor/head/marker. One exact match completes the original action; no match, read error, moved head, conflicting matches or pagination cap keep it unresolved. No second POST is attempted. Done actions cannot be downgraded or deleted by late unknown/failure handling. This intentionally favors manual investigation over duplicate comments.

## Verification and deployment holds

New boundary and ledger tests run with mocked external transports; existing test fixtures now separately mock App transport so regression cannot post real reviews. Real integration testing is identity-only through this candidate's pinned Client.

Known baseline test failure: `SkillTest.test_missing_steps_come_from_executed_commands_only` on Windows, reproduced against untouched 25fc4d9 exported to an isolated temp directory. That module and its behavior are not modified here; full regression is reported with this failure, not described as green.

Known installed-plugin compatibility issue: 1.2.1 rejects Draft reviews, while Inbox allows explicitly requested Draft review. Python preserves that existing eligibility and does not invent another Draft gate; the installed plugin still fails closed with no gh fallback. This must be resolved or explicitly assessed before deployment; the plugin owner has been notified. Do not change the pinned version/hash without verifying the replacement and repeating candidate tests/QQ review as appropriate.

Ruff is absent and an offline cached-tool attempt cannot resolve it. Syntax compilation and whitespace diff checks are available; no claim of lint success is made.

QQ review and necessary acceptance remain required. Future A/B approvals and RSI clerk writes are outside this COMMENT-only candidate and do not alter its event boundary.
