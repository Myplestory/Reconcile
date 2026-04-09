"""Detector: branch deleted shortly before card marked Complete."""

from __future__ import annotations

from datetime import timedelta

from reconcile.schema import Event, Alert, is_complete_column
from .base import BaseDetector


class BranchDeleteCompleteDetector(BaseDetector):
    name = "branch-delete-before-complete"
    description = "Branch deleted within N seconds before card moved to Complete"
    category = "evidence"

    def __init__(self, window_seconds: int = 300):
        super().__init__()
        self.window = timedelta(seconds=window_seconds)

    def get_config(self) -> dict:
        return {"window_seconds": int(self.window.total_seconds())}

    def _init_team_state(self) -> dict:
        return {
            "recent_deletions": {},  # branch_name -> (timestamp, actor)
            "card_branches": {},     # card_id -> branch_name
        }

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)

        if event.action == "branch.delete":
            s["recent_deletions"][event.target] = (event.timestamp, event.actor)

        if event.action == "card.tag" and "branch:" in str(event.metadata.get("tag", "")):
            branch = str(event.metadata["tag"]).replace("branch:", "").strip()
            s["card_branches"][event.target] = branch

        if event.action == "card.move":
            to_pipeline = str(event.metadata.get("to_pipeline_name", event.metadata.get("to_pipeline", "")))
            if is_complete_column(to_pipeline):
                card_id = event.target
                branch = s["card_branches"].get(card_id, "")
                if branch and branch in s["recent_deletions"]:
                    del_time, del_actor = s["recent_deletions"][branch]
                    gap = event.timestamp - del_time
                    if timedelta(0) <= gap <= self.window:
                        alerts.append(self.alert(
                            severity="critical",
                            title=f"Branch '{branch}' deleted {gap.total_seconds():.0f}s before card {card_id} completed",
                            detail=(
                                f"Branch '{branch}' was deleted by {del_actor} at {del_time.isoformat()}. "
                                f"Card {card_id} was moved to Complete by {event.actor} at {event.timestamp.isoformat()}, "
                                f"{gap.total_seconds():.0f} seconds later. "
                                f"The evidence container was destroyed before credit was claimed."
                            ),
                            team_id=event.team_id,
                            event_ids=[id(event)],
                        ))

        return alerts
