"""Base ingestor. All ingestors inherit from this."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from reconcile.bus import EventBus

log = logging.getLogger(__name__)


class BaseIngestor:
    """Async ingestor base. Subclasses implement stream()."""

    def __init__(self):
        self._bus: EventBus | None = None

    def set_bus(self, bus: EventBus) -> None:
        self._bus = bus

    async def emit(self, event) -> None:
        """Push a normalized event to the bus."""
        if self._bus:
            await self._bus.publish(event)

    async def stream(self) -> None:
        raise NotImplementedError
