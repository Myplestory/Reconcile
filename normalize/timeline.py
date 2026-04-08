"""
Unified timeline builder — merges events from all sources into chronological order.
"""

from __future__ import annotations

from datetime import timezone
from .types import Event


def _sort_key(e: Event):
    """Sort key that handles mixed tz-aware and naive datetimes."""
    ts = e.timestamp
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


class Timeline:
    """Chronological event stream from all sources."""

    def __init__(self):
        self.events: list[Event] = []

    def build(self, events: list[Event]) -> None:
        """Sort all events by timestamp, deduplicate by (timestamp, source, entity_id)."""
        seen = set()
        unique = []
        for e in events:
            key = (e.timestamp, e.source, e.entity_id)
            if key not in seen:
                seen.add(key)
                unique.append(e)
        self.events = sorted(unique, key=_sort_key)

    def window(self, start, end) -> list[Event]:
        """Return events within a time window."""
        return [e for e in self.events if start <= e.timestamp <= end]

    def by_actor(self, actor: str) -> list[Event]:
        """Return events for a specific actor."""
        return [e for e in self.events if e.actor == actor]

    def by_source(self, source: str) -> list[Event]:
        """Return events from a specific source."""
        return [e for e in self.events if e.source == source]

    def by_date(self, date_str: str) -> list[Event]:
        """Return events on a specific date (YYYY-MM-DD)."""
        return [e for e in self.events if e.timestamp.strftime("%Y-%m-%d") == date_str]

    def __len__(self):
        return len(self.events)

    def __iter__(self):
        return iter(self.events)
