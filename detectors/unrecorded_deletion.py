"""Detector: branch deleted in git with no corresponding board record."""

from __future__ import annotations

from datetime import timedelta

from reconcile.schema import Event, Alert
from .base import BaseDetector


class UnrecordedDeletionDetector(BaseDetector):
    name = "unrecorded-deletion"
    description = "Branch deleted in git with no corresponding board unlink/delete event"
    category = "evidence"

    def __init__(self, window_seconds: int = 600):
        super().__init__()
        self.window = timedelta(seconds=window_seconds)

    def get_config(self) -> dict:
        return {"window_seconds": int(self.window.total_seconds())}

    def _init_team_state(self) -> dict:
        return {
            "board_deletions": {},  # branch_name -> timestamp (from board events)
            "git_deletions": {},    # branch_name -> (timestamp, actor)
        }

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)

        # Track board-side branch removal events
        if event.action in ("card.unlink", "card.untag") and event.source != "git":
            tag = str(event.metadata.get("tag", ""))
            if "branch:" in tag:
                branch = tag.replace("branch:", "").strip()
                s["board_deletions"][branch] = event.timestamp

        # Track git-side branch deletions
        if event.action == "branch.delete" and event.source == "git":
            branch = event.target
            s["git_deletions"][branch] = (event.timestamp, event.actor)

            # Check if board recorded this deletion (within window)
            board_time = s["board_deletions"].get(branch)
            if board_time is None:
                alerts.append(self.alert(
                    severity="elevated",
                    title=f"Branch '{branch}' deleted in git with no board record",
                    detail=(
                        f"Branch '{branch}' was deleted by {event.actor} at "
                        f"{event.timestamp.isoformat()} in git, but no corresponding "
                        f"board unlink or tag removal event was found."
                    ),
                    team_id=event.team_id,
                    event_ids=[id(event)],
                ))

        return alerts
