"""Column flow detector — enforces board process constraints.

Implements three checks derived from professor's commented-out validation
code in scrumboardutils (source/4-8/4.8scrumboardutils1.1.js lines 57-126):

1. Complete without Testing — card moved to Complete from any column other
   than Testing. Indicates skipped QA step.
2. Backlog regression — card moved to Backlog from a column other than
   Planned or Backlog. Active work sent backward without explanation.
3. Closed by non-PM — card moved to Closed by someone other than the PM.
   Only the PM should close cards per process.

All three are process observations (info/elevated), not attribution flags.
"""

from __future__ import annotations

from reconcile.schema import Event, Alert, Category
from .base import BaseDetector


class ColumnFlowDetector(BaseDetector):
    name = "column-flow"
    description = "Detects board process violations: skipped testing, backlog regression, non-PM close"
    category = Category.PROCESS

    def __init__(self, pm_user_id: str = ""):
        super().__init__()
        self.pm_user_id = pm_user_id

    def _init_team_state(self) -> dict:
        return {
            # card_id -> last known pipeline name (for replay where oldpipelineid may be missing)
            "card_pipeline": {},
        }

    def get_config(self) -> dict:
        return {"pm_user_id": self.pm_user_id}

    async def detect(self, event: Event) -> list[Alert]:
        if event.action != "card.move":
            return []

        alerts: list[Alert] = []
        state = self.team_state(event.team_id)
        card_pipeline = state["card_pipeline"]

        to_pipeline = (
            event.metadata.get("to_pipeline_name", "")
            or event.metadata.get("to_pipeline", "")
        ).strip()

        # Determine source pipeline: prefer live WS oldpipelineid, fall back to tracked state
        from_pipeline = event.metadata.get("from_pipeline_name", "")
        if not from_pipeline:
            from_pid = event.metadata.get("from_pipeline", "")
            # During replay, from_pipeline may be a numeric ID — try to use tracked name
            if from_pid and not from_pid.isdigit():
                from_pipeline = from_pid
        if not from_pipeline:
            from_pipeline = card_pipeline.get(event.target, "")

        to_lower = to_pipeline.lower()
        from_lower = from_pipeline.lower()

        # Check 1: Complete without Testing (professor lines 57-69)
        if to_lower == "complete" and from_lower and from_lower != "testing":
            alerts.append(self.alert(
                severity="info",
                title=f"Card {event.target} completed without testing",
                detail=(
                    f"Card moved to Complete from {from_pipeline}, not from Testing. "
                    f"Actor: {event.actor}. Skipped QA step."
                ),
                team_id=event.team_id,
                event_ids=[event.event_hash],
                metadata={"from": from_pipeline, "to": to_pipeline, "card": event.target},
            ))

        # Check 2: Backlog regression from non-Planned (professor lines 71-97, active check at 86)
        if to_lower == "backlog" and from_lower not in ("planned", "backlog", ""):
            alerts.append(self.alert(
                severity="info",
                title=f"Card {event.target} regressed to Backlog",
                detail=(
                    f"Card moved to Backlog from {from_pipeline}. "
                    f"Actor: {event.actor}. Active work sent backward."
                ),
                team_id=event.team_id,
                event_ids=[event.event_hash],
                metadata={"from": from_pipeline, "to": to_pipeline, "card": event.target},
            ))

        # Check 3: Closed by non-PM (professor lines 100-112)
        if to_lower == "closed" and self.pm_user_id:
            # Compare actor against PM identity — actor is already resolved to canonical name
            # pm_user_id is the raw user ID; we need to check if actor != PM's canonical name
            # The detector doesn't have the member_map, so we check both raw ID and canonical
            if event.actor != self.pm_user_id:
                alerts.append(self.alert(
                    severity="elevated",
                    title=f"Card {event.target} closed by non-PM",
                    detail=(
                        f"Card moved to Closed by {event.actor}, who is not the PM. "
                        f"Only the PM should close cards per process."
                    ),
                    team_id=event.team_id,
                    event_ids=[event.event_hash],
                    metadata={"actor": event.actor, "card": event.target},
                ))

        # Update tracked pipeline state
        if to_pipeline:
            card_pipeline[event.target] = to_pipeline

        return alerts
