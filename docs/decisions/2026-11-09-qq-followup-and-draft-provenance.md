# QQ follow-up: source labels, page merge and Draft provenance

Status: candidate only, pending QQ review; no production changes.

## Confirmed findings versus automatic review

Automatic body/inline output retains the fixed Review Agent notice. Human-confirmed findings use the separate fixed first line:

`[ReviewInboxOppieXYZ] 人工在 Inbox 确认后发布的发现，不是 QQ 的批准`

This records the Inbox confirmation path, not the operator's identity. It never names a human reviewer or means formal approval. Both paths remain App COMMENT with a full head and unique marker. Historical reviews are not rewritten.

## Page actions

Both approve and merge are forbidden before card lookup or Pipeline invocation, even with a valid page token and claimed roles/enable flags. Dedicated exceptions return 403 FORMAL_APPROVAL_DISABLED or FORMAL_MERGE_DISABLED. Both sets of UI buttons are removed, and doAction refuses both as well. Existing cards may display readiness; QQ must use the separate authorized GitHub workflow/queue. This change does not grant or implement an automatic classifier or change App rights. The reserved approval outlet is still inert.

## Runtime configuration

`review_app.entry`, `real_entry` and `node_executable` must be explicit absolute deployment paths in the private runtime config; defaults/example contain empty paths. The projection is resolved strictly and compared to the accepted real entry. Its SHA256 must equal both the runtime config and the currently reviewed code pin. Node is invoked by its resolved absolute path, never via PATH or shell. No user-specific installation path remains in review_app.py. Missing paths stop before process launch; changing engines/projections/bundles stops calls until the replacement is verified. No credentials belong in this configuration.

The only accepted bundle remains 1.2.1, SHA256 c7681c8930c7c6aab68b91518513b1032cf9c43d73e30ba64d4d48beb6aa044c. A new plugin candidate is not installation evidence and does not authorize changing the pin. Tests and identity smoke use in-memory settings, without writing production config. Code-pin version changes and runtime settings must be reviewed together before deployment.

## Draft source declarations

The optional field is an internal service declaration, not host-attested identity and not a model tool/prompt argument. Client syntax validation alone is not source verification. Pipeline derives a reference only from:

- a stored manual requester whose id exactly matches this PR;
- a stored explicit Slack review message associated with this PR, matching requester/user/author/workspace/channel metadata and service review-intent classification;
- a service-generated page findings/request_changes action event, bound to the full current head and looked up from the timeline. HTTP body draft_request is ignored.

Automatic discovery or model text does not generate a reference. The reference and human-confirmation provenance are persisted with the claim before submission and retained after success; unknown outcomes still use the existing marker and read-only reconciliation, not a second POST.

Explicit DM asks now receive a stored mention before requester registration. Mention storage adds an optional user_id column; newly promoted explicit asks retain review classification. Legacy records with missing user identity are not guessed or retroactively attested: they remain ineligible for an override until fresh verified source evidence or an explicit page action exists. Link-only or automatic Slack discovery does not gain an override merely because the old Inbox treated it as eligible for review.

Only a live Draft with verified source gets draft_request in submit_review's args. Non-Drafts omit it, preserving pinned 1.2.1 compatibility. The installed 1.2.1 rejects the new field/Draft review, so this candidate **does not restore live Draft publishing yet**. Wait for QQ review of the plugin candidate, installation, and independent realpath/hash/identity acceptance; no shared-gh fallback. The plugin is responsible for not forwarding the field to GitHub and enforcing account=reviewinbox/event=COMMENT only; its new behavior is not asserted as installed here.

No merge queue entry, approval, comment submission or deployment was used to test these changes.
