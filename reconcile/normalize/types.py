"""
Normalized data types — the lingua franca of the Reconcile pipeline.

Every ingest module produces these types. Every analysis module consumes them.
Raw source data is preserved in the `raw` field for audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class Event:
    """Universal event. Every source record normalizes to this."""

    timestamp: datetime          # UTC, always
    source: str                  # "git" | "board" | "discord" | "email" | "source"
    actor: str                   # Canonical member name
    action: str                  # Normalized action type
    entity_id: str               # Card #, branch name, file path, message ID
    detail: str                  # Human-readable description
    raw: dict = field(default_factory=dict, repr=False)  # Original record


@dataclass
class Commit:
    """Git commit."""

    sha: str
    author: str                  # Canonical name
    date: datetime               # UTC
    message: str
    parents: list[str] = field(default_factory=list)
    branch: str | None = None

    def to_event(self) -> Event:
        return Event(
            timestamp=self.date,
            source="git",
            actor=self.author,
            action="commit",
            entity_id=self.sha[:8],
            detail=self.message[:80],
            raw={"sha": self.sha, "parents": self.parents, "branch": self.branch},
        )


@dataclass
class FileRecord:
    """Git file authorship record."""

    path: str
    original_author: str
    original_date: datetime
    original_commit: str
    current_author: str | None = None
    duplicate_commit: str | None = None
    duplicate_date: datetime | None = None
    blob_hash: str | None = None


@dataclass
class Card:
    """Scrumboard card with full lifecycle."""

    number: int
    title: str
    created_by: str
    created_date: datetime
    assigned_to: list[str] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    moves: list[Event] = field(default_factory=list)
    members: list[Event] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class Branch:
    """Git branch with provenance."""

    name: str
    deleted: bool
    unique_commits: list[Commit] = field(default_factory=list)
    first_author: str | None = None
    fork_parent_sha: str | None = None
    fork_parent_author: str | None = None
    merged: bool = False
    on_remote: bool = False
    board_creator: str | None = None
    board_creator_date: datetime | None = None
    board_card: int | None = None


@dataclass
class Message:
    """Discord message with classification."""

    snowflake: str
    timestamp: datetime          # From Snowflake decomposition (UTC)
    author: str                  # Canonical name
    channel: str
    channel_id: str
    content: str
    tier1_categories: list[str] = field(default_factory=list)
    tier2_candidates: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    def to_event(self) -> Event:
        return Event(
            timestamp=self.timestamp,
            source="discord",
            actor=self.author,
            action="message",
            entity_id=self.snowflake,
            detail=self.content[:120],
            raw=self.raw,
        )


@dataclass
class Report:
    """Status report email with member markings."""

    linkid: int
    date: datetime               # SMTP Date header (UTC)
    meeting_date: str            # Human-readable from email body
    source_file: str             # .eml or .msg filename
    members: dict[str, MemberMarking] = field(default_factory=dict)
    raw_headers: dict = field(default_factory=dict, repr=False)

    def to_event(self) -> Event:
        return Event(
            timestamp=self.date,
            source="email",
            actor="system",
            action="status_report",
            entity_id=str(self.linkid),
            detail=f"Status report for {self.meeting_date}",
            raw={"linkid": self.linkid, "source_file": self.source_file},
        )


@dataclass
class MemberMarking:
    """Individual member's marking in a status report."""

    name: str
    attendance: str              # "On Time" | "Late" | "Absent"
    prepared: str                # "Prepared" | "Unprepared"
    ontime_cumulative: int = 0
    late_cumulative: int = 0
    absent_cumulative: int = 0
    score: int = 0


@dataclass
class Observation:
    """A divergence detected by an invariant check."""

    invariant: str               # e.g., "attribution-preservation"
    evidence_quality: str        # "git-verifiable" | "board-verifiable" | "verbal-possible"
    date: datetime | None = None
    description: str = ""
    entities: list[str] = field(default_factory=list)  # Card #s, branch names, file paths
    actors: list[str] = field(default_factory=list)     # Members involved
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class SnapshotDiff:
    """Result of comparing a live git repo against a captured bundle."""

    refs_added: list[str] = field(default_factory=list)
    refs_removed: list[str] = field(default_factory=list)
    refs_diverged: list[tuple[str, str, str]] = field(default_factory=list)  # (name, bundle_sha, live_sha)
    objects_missing: list[str] = field(default_factory=list)  # SHAs in bundle not in live
    bundle_hash: str = ""
    capture_date: datetime | None = None


@dataclass
class PipelineState:
    """Complete state of the pipeline after all phases. Passed between phases."""

    # Ingest outputs
    commits: list[Commit] = field(default_factory=list)
    branches: list[Branch] = field(default_factory=list)
    files: list[FileRecord] = field(default_factory=list)
    board_events: list[Event] = field(default_factory=list)
    cards: list[Card] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    reports: list[Report] = field(default_factory=list)
    timeline: list[Event] = field(default_factory=list)
    snapshot_diff: SnapshotDiff | None = None

    # Analysis outputs
    observations: list[Observation] = field(default_factory=list)
    dag: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    scores: dict[str, Any] = field(default_factory=dict)
    pairs: dict[str, Any] = field(default_factory=dict)
    classifications: dict[str, Any] = field(default_factory=dict)

    # Forensics outputs
    snowflake_validation: dict[str, Any] = field(default_factory=dict)
    email_analysis: dict[str, Any] = field(default_factory=dict)
    consent_results: dict[str, Any] = field(default_factory=dict)
    manifest: dict[str, str] = field(default_factory=dict)

    # Raw intermediate artifacts (dicts passed between phases)
    raw_artifacts: dict[str, Any] = field(default_factory=dict)
