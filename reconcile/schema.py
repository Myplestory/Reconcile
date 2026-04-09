"""Core event and alert schema. Zero dependencies beyond stdlib.

Universal schema — same types regardless of source tool (board tools, Jira,
Trello, GitHub, GitLab, Discord, Slack, email). Ingestors normalize into
this schema; detectors and analyzers only see these types.

Action vocabulary (normalized):
    Card lifecycle:    card.create, card.update, card.delete, card.move
    Card membership:   card.assign, card.unassign
    Card metadata:     card.tag, card.untag, card.link, card.unlink
    Branch lifecycle:  branch.create, branch.delete
    Commit:            commit.create, commit.push
    File:              file.create, file.modify, file.delete
    PR:                pr.open, pr.merge, pr.close, pr.review
    Message:           message.send, message.edit, message.delete
    Report:            report.submit, report.revise
    Session:           session.join, session.leave, session.presence,
                       session.users, session.present, session.absent

Priority:
    "high" — state-changing actions (moves, deletes, commits, assigns)
    "low"  — read/presence/update actions (access, load, presence)

Confidence:
    "server-authoritative"  — timestamp/actor from server (WS, webhook)
    "client-reported"       — timestamp/actor from client (self-reported)
    "inferred"              — derived from git log, email headers, etc.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    """Normalized event from any source. Immutable after creation."""

    timestamp: datetime
    source: str          # "board-ws" | "git" | "discord" | "email" | "github"
    team_id: str         # partition key — all routing uses this
    actor: str           # member ID or "system"
    action: str          # normalized action (see vocabulary above)
    target: str          # card number, branch name, file path, etc.
    target_type: str     # "card" | "branch" | "commit" | "file" | "report" | "session" | "pr"
    metadata: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False)
    confidence: str = "server-authoritative"
    priority: str = "high"  # "high" | "low" — routes to priority queue

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @property
    def event_hash(self) -> str:
        """Content-addressable hash. Same logical event → same hash.

        Covers: timestamp + source + team_id + actor + action + target + metadata.
        Does NOT include raw (which may vary) or ingestion-time fields.
        """
        content = json.dumps({
            "ts": self.timestamp.isoformat(),
            "src": self.source,
            "tid": self.team_id,
            "act": self.actor,
            "action": self.action,
            "tgt": self.target,
            "meta": self.metadata,
        }, sort_keys=True, default=str)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


# Actions that indicate state-changing operations (high priority by default)
HIGH_PRIORITY_ACTIONS = frozenset({
    "card.create", "card.delete", "card.move",
    "card.assign", "card.unassign",
    "card.tag", "card.untag", "card.link", "card.unlink",
    "branch.create", "branch.delete",
    "commit.create", "commit.push",
    "file.create", "file.modify", "file.delete",
    "pr.open", "pr.merge",
    "message.send", "message.delete",
    "report.submit", "report.revise",
})

# Actions that are informational / read-only (low priority)
LOW_PRIORITY_ACTIONS = frozenset({
    "card.update", "card.access",
    "pr.close", "pr.review",
    "message.edit",
    "session.join", "session.leave", "session.presence", "session.users",
    "session.present", "session.absent",
    "board.load",
})


def default_priority(action: str) -> str:
    """Derive priority from action if not explicitly set."""
    if action in HIGH_PRIORITY_ACTIONS:
        return "high"
    if action in LOW_PRIORITY_ACTIONS:
        return "low"
    return "high"  # unknown actions default to high (don't miss them)


# --- Violation categories ---
# Classifies WHAT kind of integrity issue an alert represents.
# Determines how the alert is weighted, displayed, and escalated.

class Category:
    """Violation category constants. From lowest to highest institutional impact."""

    PROCESS = "process"
    """Process deviation — didn't follow workflow but no integrity harm.
    Examples: card completed by non-assignee, batch completions, missing board record.
    Institutional: course process standards."""

    ATTENDANCE = "attendance"
    """Attendance integrity — presence/absence discrepancies.
    Examples: marked present with no activity, unexcused absences, report revision.
    Institutional: attendance policy, participation grading."""

    ATTRIBUTION = "attribution"
    """Attribution manipulation — work credited to wrong person.
    Examples: file re-added under different author, zero-commit completion.
    Institutional: academic integrity, individual grading."""

    EVIDENCE = "evidence"
    """Evidence destruction — deliberate removal of audit trail.
    Examples: branch deleted before card completed, evidence container destroyed.
    Institutional: academic integrity, obstruction."""


# Severity weight by category — same severity string means different things
# in different categories. A "suspect" process violation is less severe than
# a "suspect" evidence destruction.
CATEGORY_WEIGHT = {
    Category.PROCESS: 1,
    Category.ATTENDANCE: 2,
    Category.ATTRIBUTION: 3,
    Category.EVIDENCE: 4,
}

SEVERITY_WEIGHT = {
    "info": 1,
    "elevated": 2,
    "suspect": 3,
    "critical": 4,
}


def composite_score(category: str, severity: str) -> int:
    """Compute a composite severity score: category_weight * severity_weight.

    Range: 1 (info process) to 16 (critical evidence).
    Use for sorting, thresholding, and dashboard display.
    """
    return CATEGORY_WEIGHT.get(category, 1) * SEVERITY_WEIGHT.get(severity, 1)


@dataclass(slots=True)
class Alert:
    """Detector output. One alert per anomaly detected."""

    detector: str        # detector module name
    severity: str        # "info" | "elevated" | "suspect" | "critical"
    category: str = Category.PROCESS  # violation category
    title: str = ""      # one-line summary
    detail: str = ""     # full description with evidence refs
    team_id: str = ""    # which team this alert belongs to
    event_ids: list = field(default_factory=list)
    timestamp: datetime = field(default_factory=Event.now)
    metadata: dict = field(default_factory=dict)

    @property
    def score(self) -> int:
        """Composite severity score (1–16). Higher = more severe."""
        return composite_score(self.category, self.severity)


# --- Column name normalization ---
# Different tools use different names for "done" columns.
# Detectors should check against COMPLETE_COLUMN_NAMES, not hardcoded IDs.

COMPLETE_COLUMN_NAMES = frozenset({
    "complete", "done", "finished", "closed", "resolved",
    "merged", "deployed", "released",
})


def is_complete_column(name: str) -> bool:
    """Check if a column/pipeline name represents task completion.

    Handles both human-readable names and numeric pipeline IDs
    that ingestors tag via metadata['to_pipeline_name'].
    """
    return name.strip().lower() in COMPLETE_COLUMN_NAMES
