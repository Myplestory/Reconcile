"""Detector: status report revised with different accountability markings."""

from __future__ import annotations

from reconcile.schema import Event, Alert
from .base import BaseDetector


class ReportRevisionDetector(BaseDetector):
    name = "report-revision"
    description = "Status report for same period submitted with different markings"
    category = "attendance"

    def _init_team_state(self) -> dict:
        return {
            "reports": {},  # (period, actor) -> {markings, timestamp, count}
        }

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)

        if event.action not in ("report.submit", "report.revise"):
            return alerts

        period = event.metadata.get("period", event.target)
        actor = event.actor
        markings = event.metadata.get("markings", "")
        key = (period, actor)

        if key in s["reports"]:
            prev = s["reports"][key]
            prev_markings = prev["markings"]
            if markings and prev_markings and markings != prev_markings:
                prev["count"] += 1
                alerts.append(self.alert(
                    severity="suspect",
                    title=f"Report for '{period}' revised by {actor} (revision #{prev['count']})",
                    detail=(
                        f"Status report for period '{period}' was revised by {actor}. "
                        f"Previous markings differ from current submission. "
                        f"Original at {prev['timestamp'].isoformat()}, "
                        f"revision at {event.timestamp.isoformat()}."
                    ),
                    team_id=event.team_id,
                    event_ids=[id(event)],
                ))
            prev["markings"] = markings
            prev["timestamp"] = event.timestamp
        else:
            s["reports"][key] = {
                "markings": markings,
                "timestamp": event.timestamp,
                "count": 0,
            }

        return alerts
