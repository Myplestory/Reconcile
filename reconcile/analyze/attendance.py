"""Attendance from PM status reports (authoritative) + activity-cluster fallback.

Primary: Parse PM-generated status report emails for ground-truth attendance.
Fallback: Infer meetings from observable activity clusters (heuristic, less reliable).
"""
from __future__ import annotations

import base64
import email
import glob
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..config import PipelineConfig
from ..normalize.types import Event, Observation


# ---------------------------------------------------------------------------
# PM Status Report Parser (authoritative)
# ---------------------------------------------------------------------------

def parse_status_reports(email_dir: str) -> dict[str, dict]:
    """Parse PM status report .eml files for authoritative attendance.

    Returns:
        {member_name: {"present": int, "absent": int, "meetings": [{date, status, raw}]}}
    """
    records: dict[str, dict] = {}
    eml_files = sorted(glob.glob(os.path.join(email_dir, "*.eml")))

    for path in eml_files:
        # Extract date from filename: status-report_YYYY-MM-DD_*.eml
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", os.path.basename(path))
        if not date_match:
            continue
        report_date = date_match.group(1)

        # Decode email body
        try:
            with open(path) as f:
                msg = email.message_from_string(f.read())
            payload = msg.get_payload()
            if isinstance(payload, str):
                body = base64.b64decode(payload).decode("utf-8", errors="replace")
            else:
                continue
        except Exception:
            continue

        # Strip HTML → plain text
        text = re.sub(r"<[^>]+>", " ", body)
        text = re.sub(r"\s+", " ", text).strip()

        # Extract Name/Marked-as pairs
        entries = re.findall(
            r"Name\s*:\s*(.+?)\s+Task.*?Marked as\s*:\s*([^\[]+)", text
        )
        for raw_name, raw_status in entries:
            name = raw_name.strip()
            status = raw_status.strip()
            if name not in records:
                records[name] = {"present": 0, "absent": 0, "meetings": []}
            is_absent = status.lower().startswith("absent")
            if is_absent:
                records[name]["absent"] += 1
            else:
                records[name]["present"] += 1
            records[name]["meetings"].append({
                "date": report_date,
                "status": status,
                "absent": is_absent,
            })

    return records


@dataclass
class InferredMeeting:
    """A time window where 3+ members were active."""
    start: datetime
    end: datetime
    members_active: list[str]
    members_absent: list[str]
    source_counts: dict[str, int] = field(default_factory=dict)  # member → activity count


@dataclass
class AttendanceRecord:
    """Per-member attendance summary."""
    member: str
    meetings_present: int = 0
    meetings_absent: int = 0
    absence_rate: float = 0.0
    meetings: list[dict] = field(default_factory=list)  # [{date, status, sources}]


def infer_meetings(
    events: list[Event],
    all_members: list[str],
    window_minutes: int = 60,
    min_members: int = 3,
) -> list[InferredMeeting]:
    """Find time windows where min_members+ are active within window_minutes.

    Groups activity into non-overlapping windows. Conservative: only flags windows
    where multiple members clearly overlapped.
    """
    if not events or not all_members:
        return []

    # Sort events by timestamp
    sorted_events = sorted(events, key=lambda e: e.timestamp)
    window = timedelta(minutes=window_minutes)

    # Sliding window: find clusters of activity from different members
    meetings: list[InferredMeeting] = []
    i = 0
    used_dates: set[str] = set()  # prevent duplicate meetings on same day

    while i < len(sorted_events):
        window_start = sorted_events[i].timestamp
        window_end = window_start + window

        # Collect all events in this window
        members_in_window: dict[str, int] = defaultdict(int)
        j = i
        while j < len(sorted_events) and sorted_events[j].timestamp <= window_end:
            actor = sorted_events[j].actor
            if actor in all_members:
                members_in_window[actor] += 1
            j += 1

        # Check if enough distinct members were active
        if len(members_in_window) >= min_members:
            day_key = window_start.strftime("%Y-%m-%d")
            if day_key not in used_dates:
                used_dates.add(day_key)
                active = list(members_in_window.keys())
                absent = [m for m in all_members if m not in members_in_window]
                meetings.append(InferredMeeting(
                    start=window_start,
                    end=window_end,
                    members_active=active,
                    members_absent=absent,
                    source_counts=dict(members_in_window),
                ))
            # Skip past this window
            i = j
        else:
            i += 1

    return meetings


def compute_attendance(
    events: list[Event],
    all_members: list[str],
    config: PipelineConfig | None = None,
    window_minutes: int = 60,
    min_members: int = 3,
) -> dict:
    """Full attendance analysis. Returns serializable dict.

    Output:
        {
            "meetings": [...],
            "members": {name: AttendanceRecord},
            "summary": {name: {present, absent, rate}}
        }
    """
    meetings = infer_meetings(events, all_members, window_minutes, min_members)

    # Build per-member records
    records: dict[str, AttendanceRecord] = {m: AttendanceRecord(member=m) for m in all_members}

    for meeting in meetings:
        date_str = meeting.start.strftime("%Y-%m-%d %H:%M")
        for member in meeting.members_active:
            if member in records:
                r = records[member]
                r.meetings_present += 1
                r.meetings.append({
                    "date": date_str,
                    "status": "present",
                    "activity_count": meeting.source_counts.get(member, 0),
                })
        for member in meeting.members_absent:
            if member in records:
                r = records[member]
                r.meetings_absent += 1
                r.meetings.append({
                    "date": date_str,
                    "status": "absent",
                    "activity_count": 0,
                })

    # Compute absence rates
    for r in records.values():
        total = r.meetings_present + r.meetings_absent
        r.absence_rate = round(r.meetings_absent / total, 2) if total > 0 else 0.0

    # Serialize
    return {
        "inferred_meetings": [
            {
                "date": m.start.strftime("%Y-%m-%d"),
                "start": m.start.isoformat(),
                "end": m.end.isoformat(),
                "active": m.members_active,
                "absent": m.members_absent,
                "activity_counts": m.source_counts,
            }
            for m in meetings
        ],
        "members": {
            name: {
                "member": r.member,
                "meetings_present": r.meetings_present,
                "meetings_absent": r.meetings_absent,
                "absence_rate": r.absence_rate,
                "meetings": r.meetings,
            }
            for name, r in sorted(records.items(), key=lambda x: -x[1].absence_rate)
        },
        "total_inferred_meetings": len(meetings),
        "window_minutes": window_minutes,
        "min_members_for_meeting": min_members,
    }
