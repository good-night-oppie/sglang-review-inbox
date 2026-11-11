"""Chinese verdict label for a ReviewInbox review summary.

Pure function only: no I/O, no clock, no config, no side effects. The caller
owns everything that needs state: it must reject a stale or superseded head
before publishing, and it must pass the findings it is actually about to
publish as `wanted` (already narrowed to this head and this publication path,
human or agent).

Decision table (review["verdict"] is the current-head effective verdict,
findings follow REVIEW_SCHEMA + VERIFY_SCHEMA: severity blocker/major/minor/nit,
verify confirmed/plausible/rejected):

| confirmed blocker/major bug | plausible (unverified) blocker/major | verdict        | label   |
| --------------------------- | ------------------------------------ | -------------- | ------- |
| yes                         | any                                  | any            | 要改    |
| no                          | yes                                  | any            | 需要讨论 |
| no                          | no                                   | changes_needed | 需要讨论 |
| no                          | no                                   | needs_human    | 需要讨论 |
| no                          | no                                   | lgtm / minor   | 通过    |

Any malformed or contradictory input (unknown verdict, missing severity, a
blocking finding without a verify state, findings that are not mappings)
returns 需要讨论. The verify field of non-blocking findings (minor/nit,
kind=evidence) is ignored. It never defaults to 通过, because a broken record
is a question for a human, not a pass.
"""
from __future__ import annotations

import re

VERDICTS = ("lgtm", "minor", "changes_needed", "needs_human")
SEVERITIES = ("blocker", "major", "minor", "nit")
VERIFY_STATES = ("confirmed", "plausible", "rejected")

EVIDENCE_CATEGORY = re.compile(r"^(tests?|validation|hardware-validation)$|^tests?[/-]|evidence|benchmark|claim", re.I)


def is_risk(finding):
    """One shared definition of a concrete defect (agent.is_risk delegates here).

    A finding with an explicit kind is a risk only when it is a bug; without a
    kind, test/evidence/benchmark categories never block.
    """
    kind = finding.get("kind")
    if kind:
        return kind == "bug"
    return not EVIDENCE_CATEGORY.search(finding.get("category") or "")

PASS = "通过"
CHANGES = "要改"
DISCUSS = "需要讨论"


def _blocking(finding):
    """True for blocker/major defects under the shared risk rule."""
    return finding.get("severity") in ("blocker", "major") and is_risk(finding)


def review_verdict_label(review, wanted):
    """Return exactly one of 通过 / 要改 / 需要讨论.

    `review` is the stored review record for the current head; `wanted` is the
    list of findings this publication will actually carry. Malformed input is
    a 需要讨论, never a pass.
    """
    if not isinstance(review, dict):
        return DISCUSS
    verdict = review.get("verdict")
    if verdict not in VERDICTS:
        return DISCUSS
    if wanted is None:
        return DISCUSS
    if not isinstance(wanted, (list, tuple)):
        return DISCUSS

    confirmed = False
    plausible = False
    for finding in wanted:
        if not isinstance(finding, dict):
            return DISCUSS
        if finding.get("severity") not in SEVERITIES:
            return DISCUSS
        if not _blocking(finding):
            continue
        state = finding.get("verify")
        if state == "confirmed":
            confirmed = True
        elif state == "plausible":
            plausible = True
        elif state == "rejected":
            continue
        else:
            return DISCUSS  # a blocking finding with no verify state is a conflict

    if confirmed:
        return CHANGES
    if plausible or verdict in ("needs_human", "changes_needed"):
        return DISCUSS
    return PASS
