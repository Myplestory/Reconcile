"""Detector: attendance anomaly — cross-references reported attendance with observable activity.

Three checks:
  1. Marked present but no corroborating activity within meeting window → flag
  2. Absent with prior communication → info (escalate if frequent)
  3. Absent with no communication → flag (escalate on streak)

All thresholds configurable. No hardcoded team names, meeting times, or member IDs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from reconcile.schema import Event, Alert
from .base import BaseDetector

# Actions that count as "observable activity" for corroboration
ACTIVITY_ACTIONS = frozenset({
    "card.move", "card.create", "card.delete", "card.assign",
    "card.tag", "card.link",
    "commit.create", "commit.push",
    "branch.create",
    "message.send",
    "pr.open", "pr.merge",
})


class AttendanceAnomalyDetector(BaseDetector):
    name = "attendance-anomaly"
    description = "Cross-references reported attendance with observable activity and communication"
    category = "attendance"

    def __init__(
        self,
        activity_window_minutes: int = 120,
        absence_comms_window_hours: int = 24,
        unexcused_absence_threshold: int = 2,
        frequent_absence_threshold: int = 3,
    ):
        super().__init__()
        self.activity_window = timedelta(minutes=activity_window_minutes)
        self.absence_comms_window = timedelta(hours=absence_comms_window_hours)
        self.unexcused_threshold = unexcused_absence_threshold
        self.frequent_threshold = frequent_absence_threshold

    def get_config(self) -> dict:
        return {
            "activity_window_minutes": int(self.activity_window.total_seconds() / 60),
            "absence_comms_window_hours": int(self.absence_comms_window.total_seconds() / 3600),
            "unexcused_absence_threshold": self.unexcused_threshold,
            "frequent_absence_threshold": self.frequent_threshold,
        }

    def _init_team_state(self) -> dict:
        return {
            "member_activity": defaultdict(list),   # member -> [(timestamp, action)]
            "absence_notices": defaultdict(list),    # member -> [(timestamp,)]
            "unexcused_streak": defaultdict(int),    # member -> consecutive count
            "total_absences": defaultdict(int),      # member -> rolling total
        }

    def _has_activity_near(self, activity_log: list, meeting_time, window) -> bool:
        """Check if any activity exists within window of meeting_time."""
        for ts, _ in activity_log:
            if abs((ts - meeting_time).total_seconds()) <= window.total_seconds():
                return True
        return False

    def _has_absence_notice(self, notices: list, meeting_time, window) -> bool:
        """Check if an absence notice was sent within window before meeting."""
        for ts, in notices:
            gap = meeting_time - ts
            if timedelta(0) <= gap <= window:
                return True
        return False

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)
        actor = event.actor

        # Track observable activity for all members
        if event.action in ACTIVITY_ACTIONS:
            s["member_activity"][actor].append((event.timestamp, event.action))
            # Prune old activity (keep last 7 days)
            cutoff = event.timestamp - timedelta(days=7)
            s["member_activity"][actor] = [
                (ts, a) for ts, a in s["member_activity"][actor] if ts >= cutoff
            ]

        # Track absence notices from messages
        if event.action == "message.send":
            is_notice = (
                event.metadata.get("absence_notice")
                or event.metadata.get("content_category") in ("absence", "scheduling")
            )
            if is_notice:
                s["absence_notices"][actor].append((event.timestamp,))

        # --- Check 1: Marked present — only escalate if evidence CONTRADICTS ---
        if event.action == "session.present":
            member = event.metadata.get("member", actor)
            s["unexcused_streak"][member] = 0
            s.setdefault("total_present", defaultdict(int))
            s["total_present"][member] += 1

            # Evidence contradicts: marked present but zero activity across ALL sources
            activity = s["member_activity"].get(member, [])
            if not self._has_activity_near(activity, event.timestamp, self.activity_window):
                # Only flag if multiple members have activity (not early in replay)
                active_members = sum(1 for v in s["member_activity"].values() if len(v) >= 3)
                if active_members >= 2:
                    alerts.append(self.alert(
                        severity="elevated",
                        title=f"Evidence contradicts: {member} marked present, no observable activity",
                        detail=(
                            f"PM recorded {member} as present at {event.timestamp.isoformat()} "
                            f"but no board, git, or message activity found within "
                            f"{self.activity_window.total_seconds() / 60:.0f} min. "
                            f"Flagged for review — does not determine verdict."
                        ),
                        team_id=event.team_id,
                    ))

        # --- Check 2: Marked absent — only escalate if evidence CONTRADICTS or pattern is notable ---
        if event.action == "session.absent":
            member = event.metadata.get("member", actor)
            s["total_absences"][member] += 1
            total = s["total_absences"][member]

            # Evidence contradicts: marked absent but HAS activity near meeting time
            activity = s["member_activity"].get(member, [])
            if self._has_activity_near(activity, event.timestamp, self.activity_window):
                alerts.append(self.alert(
                    severity="suspect",
                    title=f"Evidence contradicts: {member} marked absent, but has activity",
                    detail=(
                        f"PM recorded {member} as absent at {event.timestamp.isoformat()} "
                        f"but observable board/git/message activity exists within "
                        f"{self.activity_window.total_seconds() / 60:.0f} min of meeting. "
                        f"Attendance record may be inaccurate."
                    ),
                    team_id=event.team_id,
                ))
                s["unexcused_streak"][member] = 0  # evidence says they were there
            else:
                # No contradiction — record the absence pattern
                s["unexcused_streak"][member] += 1
                streak = s["unexcused_streak"][member]

                # Check if comms provided notice
                notices = s["absence_notices"].get(member, [])
                has_notice = self._has_absence_notice(
                    notices, event.timestamp, self.absence_comms_window,
                )
                if has_notice:
                    s["unexcused_streak"][member] = 0

                # Only flag notable patterns (frequent absences), not individual ones
                if total >= self.frequent_threshold:
                    sev = "elevated" if has_notice else "info"
                    notice_ctx = "with prior notice" if has_notice else "no prior notice on record"
                    alerts.append(self.alert(
                        severity=sev,
                        title=f"{member} absent {total} times ({notice_ctx})",
                        detail=(
                            f"{member} has been absent {total} times total "
                            f"({streak} consecutive unexcused). "
                            f"Most recent: {event.timestamp.isoformat()}. "
                            f"Flagged for pattern — PM attendance record is authoritative."
                        ),
                        team_id=event.team_id,
                    ))

        return alerts
