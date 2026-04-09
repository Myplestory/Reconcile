"""Detector: card completed by someone other than assignee or PM."""

from __future__ import annotations

from reconcile.schema import Event, Alert, is_complete_column
from .base import BaseDetector


class CompletionNonAssigneeDetector(BaseDetector):
    name = "completion-non-assignee"
    description = "Card moved to Complete by someone other than assignee or PM"
    category = "process"

    def _init_team_state(self) -> dict:
        return {
            "card_assignees": {},  # card_id -> set of assigned member IDs
            "pm_users": set(),     # user IDs known to be PMs
        }

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)

        # Track PM identity from metadata
        if event.metadata.get("is_pm"):
            s["pm_users"].add(event.actor)

        # Track card assignments
        if event.action == "card.assign":
            card_id = event.target
            if card_id not in s["card_assignees"]:
                s["card_assignees"][card_id] = set()
            member = event.metadata.get("member_id", event.actor)
            s["card_assignees"][card_id].add(member)
            s["card_assignees"][card_id].add(event.actor)

        if event.action == "card.unassign":
            card_id = event.target
            member = event.metadata.get("member_id", "")
            if card_id in s["card_assignees"]:
                s["card_assignees"][card_id].discard(member)

        # Detect: card completed by non-assignee, non-PM
        if event.action == "card.move":
            to_pipeline = str(event.metadata.get("to_pipeline_name", event.metadata.get("to_pipeline", "")))
            if is_complete_column(to_pipeline):
                card_id = event.target
                assignees = s["card_assignees"].get(card_id, set())
                mover = event.actor

                if assignees and mover not in assignees and mover not in s["pm_users"]:
                    alerts.append(self.alert(
                        severity="info",
                        title=f"Card {card_id} completed by non-assignee {mover}",
                        detail=(
                            f"Card {card_id} was moved to Complete by {mover}, "
                            f"who is not in the assignee list ({', '.join(assignees)}) "
                            f"and is not a known PM."
                        ),
                        team_id=event.team_id,
                        event_ids=[id(event)],
                    ))

        return alerts
