# ReviewInbox joint scope contract — candidate, not deployed

Pairing update: the later candidate pairs source plugin PR#4 head20ef482a / version1.2.4 with bundle SHA4c306c3d...38813, verified by source-candidate identity only. Candidate code/default/example pins now use that artifact; production remains 1.2.1/c768 and blocked. The old-pin paragraphs below describe scope-only parent9152097 and are superseded for the paired candidate by the joint QA request `2026-11-09T22_45_00_joint_scope_pin_validation_request.md`.

The owner-authorized installation matrix is now six repositories. That is not publication authorization for six repositories. The deployed Inbox configuration remains MontClip and MontClip-Cindy only. This independent candidate starts at deployed 158cc6a2 and changes only Python identity validation, its fixtures, focused tests and review evidence.

## Expected identity result

`installationRepositories` must contain exactly these full names and integer IDs, once each:

| Repository under good-night-oppie | ID |
| --- | --- |
| MontClip | 1441621868 |
| MontClip-Cindy | 1446532339 |
| montclip-rsi | 1450602468 |
| rsi-playbooks | 1450603260 |
| oppie-github | 1451885379 |
| sglang-review-inbox | 1451900929 |

`repositories` must independently contain exactly MontClip 1441621868 and MontClip-Cindy 1446532339: the narrowed **execution token** scope. Missing fields, old single-list results, broader execution scopes, renamed/mismatched IDs, duplicate names or malformed entries are refused. Order is irrelevant. App 5687106, installation 180305629, owner, slug and exact permissions remain pinned. Contents/metadata remain read; issues/pull_requests remain write. COMMENT-only request validation and the two-target request allowlist do not change.

Python does not mint tokens. The coordinated plugin must verify the installation's exact-six repository set independently, request its publication token with repository_ids limited to the exact two execution IDs, and report the independently checked execution-token set as repositories. A webpage selection or caller-declared installation list is not evidence that token narrowing happened. Plugin-side token minting and receipt enforcement remain within the source task's implementation and QQ's joint C-tier review.

## Runtime hold

Production identity currently fails closed with installed 1.2.1's REPOSITORY_SCOPE_MISMATCH. The old accepted c7681c89...044c bundle SHA constant is intentionally unchanged in this candidate; production config/hash/reload and plugin source are untouched. This new contract also rejects the old identity result format. Consequently this commit alone is **not a usable restoration release**. There is no reviewed/installed six-scope plugin artifact yet.

Before deployment: obtain the paired plugin candidate, verify exact installation/execution sets and token narrowing, review together with any bundle SHA/config change as C-tier, install only the approved artifact, then verify actual realpath/hash/identity through the formal entry. Preserve the two-repository runtime allowlist and COMMENT-only policy. Draft source checks and unknown-write marker reconciliation are unchanged. No shared-gh fallback, APPROVE expansion or config-only enablement is permitted.

Existing baseline SkillTest failure and missing ruff remain explicit validation limits, not silently bypassed. No live recovery or successful joint acceptance is claimed by mocked contract tests.
