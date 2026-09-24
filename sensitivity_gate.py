"""Pre-extraction sensitivity gate.

Vendored from C:\\Work\\marketing_commercial_intelligence\\src\\commercial_intelligence\\sensitivity_gate.py
(single, self-contained, stdlib-only module -- no import coupling exists
between that repo and this one, so this is a manual copy, not a package
dependency). If the source changes, re-sync this file by hand.

Per docs/marketing-intelligence-design.md step 2 and requirements R12/R13
(mandatory boundary): HR, personal/coaching, and personnel content must never
reach the extractor at all -- this is a gate *before* extraction, not a filter
applied to already-extracted content. Nothing in agent_memory today does this;
its sensitivity tagging (routine/sensitive/hr_grade) happens *after*
extraction, as part of what the LLM decides during the observe/structure
passes. This module is genuinely new logic, not a reuse of existing code.

Fail-closed philosophy, matching the sensitivity migration's own stated rule
("Unknown fails closed; routine alone is not permission to share"): a meeting
type this gate doesn't recognize is HELD, never guessed into ELIGIBLE.

The exact boundary of what's eligible vs. excluded is a business/policy
decision, not an engineering one. This module ships with small, explicit,
clearly-labeled starter lists -- anything not on either list defaults to
HELD -- so the mechanism can be built and tested without unilaterally
deciding that boundary. Expanding either list is expected and should go
through the same review as any other policy change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

GateDecision = Literal["eligible", "held", "excluded"]

# Meeting types confirmed, by their name alone, to be HR-grade/personal/coaching
# content that must never reach the extractor. Starter list -- expand only with
# business sign-off, not silently.
EXCLUDED_MEETING_TYPES = frozenset({
    "Personal 1:1",
    "GROW",
})

# Specific recurring meeting titles confirmed personal regardless of their
# platform meeting_type -- human-confirmed exclusion, checked before the
# type-based gate below since it's absolute (a title match excludes even an
# otherwise-eligible type). Case-insensitive exact match on the stripped
# title, not a substring/keyword search -- avoid excluding an unrelated
# meeting that merely mentions these names in passing.
EXCLUDED_MEETING_TITLES = frozenset({
    "togs <> bernoske call",
    "weekly grow meeting",
})

# Meeting types confirmed to be commercial/business content safe to gate through
# to extraction (still subject to the mixed-content keyword check below).
# Starter list -- expand only with business sign-off, not silently.
ELIGIBLE_MEETING_TYPES = frozenset({
    "Prospecting Meeting",
    "Client Working Session",
    "Client Weekly Update Call",
    "Client Steering Committee Call",
    "Client Project Kick Off Call",
    "Client Pre-Wire Meeting",
    "Client Engagement",
    "Client Training Session",
    "Interview - Customer",
    "Internal Working Session",
    "Internal Project Post Mortem",
    "Mystery Shop",
    "Mystery Shop - Competitor",
    # Added 2026-09-24 alongside pull_meetings.py's IN_SCOPE_TYPES expansion:
    # sprint/task-status and project-planning content, not personnel/HR in
    # nature. "Internal Deal Desk Meeting" was considered and deliberately
    # NOT added here -- see the excluded-pending-review note below.
    "Project Scrum - Internal",
    "Project Scrum - External",
    "Internal",
    "Internal Project Kick Off",
})

# Meeting types that, based on this project's own prior findings (see
# PRIVATE_RECLASSIFICATION_PLAN.md), frequently mix personnel/candidate content
# with business content under the same name -- deliberately left off both
# lists so they default to held rather than guessed either way.
#   "Interview - Executive", "Interview - Hiring", "Interview - Staff",
#   "Internal Deal Desk Meeting", "Internal Engagement Manager Meeting",
#   "Ad Hoc/Generic Meeting"

# Mixed-sensitive check: even an otherwise-eligible meeting is held, not
# surgically filtered, if it contains personnel/HR-flavored language -- per
# the design doc, mixed-sensitive calls are held entirely in V1, not partially
# processed. Deliberately a small, conservative starter set of cues; a keyword
# heuristic, same style as BaselineExtractor's bucket cues -- needs
# calibration before being trusted at scale, same caveat as that extractor.
_MIXED_SENSITIVE_CUES = re.compile(
    r"\b(performance review|perform(?:ance)? improvement plan|\bPIP\b|"
    r"terminat(?:e|ion|ing)|salary|compensation review|write.?up|"
    r"disciplinary|hr complaint|harassment|conflict of interest)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reason: str


def classify_sensitivity(meeting: dict[str, Any]) -> GateResult:
    """Classify one meeting's eligibility for extraction. Never raises.

    meeting is expected to carry at least `meeting_type` and, when available,
    `transcript`/`transcript_clean` for the mixed-content check. Missing
    fields degrade to held, not to eligible.
    """
    title = (meeting.get("name") or "").strip().lower()
    if title in EXCLUDED_MEETING_TITLES:
        return GateResult("excluded", f"meeting_title_excluded:{title}")

    meeting_type = (meeting.get("meeting_type") or "").strip()

    if not meeting_type:
        return GateResult("held", "meeting_type_missing")

    if meeting_type in EXCLUDED_MEETING_TYPES:
        return GateResult("excluded", f"meeting_type_excluded:{meeting_type}")

    if meeting_type not in ELIGIBLE_MEETING_TYPES:
        return GateResult("held", f"meeting_type_unrecognized:{meeting_type}")

    mixed = _mixed_sensitive_hit(meeting)
    if mixed:
        return GateResult("held", f"mixed_sensitive_content:{mixed}")

    return GateResult("eligible", f"meeting_type_eligible:{meeting_type}")


def _mixed_sensitive_hit(meeting: dict[str, Any]) -> str | None:
    turns = meeting.get("transcript_clean") or meeting.get("transcript") or []
    if isinstance(turns, str):
        turns = [{"text": turns}]
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "")
        match = _MIXED_SENSITIVE_CUES.search(text)
        if match:
            return match.group(0).lower()
    return None
