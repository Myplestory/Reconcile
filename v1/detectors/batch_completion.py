"""Detector: multiple cards completed in rapid succession."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from reconcile.schema import Event, Alert, is_complete_column
from .base import BaseDetector


class BatchCompletionDetector(BaseDetector):
    name = "batch-completion"
    description = "N+ cards completed by same actor within time window"
    category = "process"

    def __init__(self, window_seconds: int = 60, min_cards: int = 3):
        super().__init__()
        self.window = timedelta(seconds=window_seconds)
        self.min_cards = min_cards

    def get_config(self) -> dict:
        return {"window_seconds": int(self.window.total_seconds()), "min_cards": self.min_cards}

    def _init_team_state(self) -> dict:
        return {"recent_completions": defaultdict(list)}  # actor -> [(timestamp, card_id)]

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)

        if event.action == "card.move":
            to_pipeline = str(event.metadata.get("to_pipeline_name", event.metadata.get("to_pipeline", "")))
            if is_complete_column(to_pipeline):
                actor = event.actor
                s["recent_completions"][actor].append((event.timestamp, event.target))

                # Prune old entries outside window
                cutoff = event.timestamp - self.window
                recent = [
                    (ts, card) for ts, card in s["recent_completions"][actor]
                    if ts >= cutoff
                ]
                s["recent_completions"][actor] = recent

                if len(recent) >= self.min_cards:
                    elapsed = (recent[-1][0] - recent[0][0]).total_seconds()
                    cards = [card for _, card in recent]
                    alerts.append(self.alert(
                        severity="info",
                        title=f"Batch: {len(recent)} cards completed by {actor} in {elapsed:.0f}s",
                        detail=(
                            f"{actor} completed {len(recent)} cards in {elapsed:.0f} seconds: "
                            f"{', '.join(cards)}. "
                            f"Window: {self.window.total_seconds():.0f}s threshold."
                        ),
                        team_id=event.team_id,
                        event_ids=[id(event)],
                    ))

        return alerts
