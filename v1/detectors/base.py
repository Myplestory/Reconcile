"""Base detector. All detectors inherit from this.

Detectors partition state by team_id internally. One detector instance
serves all teams — no per-team instantiation needed.
"""

from __future__ import annotations

from reconcile.schema import Event, Alert


class BaseDetector:
    """Stateful anomaly detector.

    Subclasses implement detect(event) → list[Alert].
    State is partitioned by team_id: self.team_state(event.team_id) returns
    the team's dict, auto-initialized on first access.
    """

    name: str = "unnamed"
    description: str = ""

    def __init__(self):
        self._state: dict[str, dict] = {}  # team_id -> detector state

    def team_state(self, team_id: str) -> dict:
        """Get or create state dict for a team."""
        if team_id not in self._state:
            self._state[team_id] = self._init_team_state()
        return self._state[team_id]

    def _init_team_state(self) -> dict:
        """Override to provide default state for a new team. Called once per team."""
        return {}

    def evict_team(self, team_id: str) -> None:
        """Free memory for archived teams."""
        self._state.pop(team_id, None)

    async def detect(self, event: Event) -> list[Alert]:
        raise NotImplementedError

    # Default category — subclasses override to classify violations
    category: str = "process"

    def get_config(self) -> dict:
        """Return current configurable parameters. Override in subclass."""
        return {}

    def alert(
        self,
        severity: str,
        title: str,
        detail: str,
        team_id: str = "",
        category: str | None = None,
        event_ids: list | None = None,
        metadata: dict | None = None,
    ) -> Alert:
        """Convenience: create an Alert from this detector."""
        return Alert(
            detector=self.name,
            severity=severity,
            category=category or self.category,
            title=title,
            detail=detail,
            team_id=team_id,
            event_ids=event_ids or [],
            metadata=metadata or {},
        )
