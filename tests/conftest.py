"""Shared fixtures for reconcile test suite."""

from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from reconcile.schema import Event
from reconcile.bus import EventBus
from reconcile.storage import Store


def event_factory(**overrides) -> Event:
    """Create an Event with sensible defaults. Override any field."""
    defaults = dict(
        timestamp=datetime.now(timezone.utc),
        source="test",
        team_id="test-team",
        actor="alice",
        action="card.move",
        target="1",
        target_type="card",
        metadata={},
        raw={},
        confidence="server-authoritative",
        priority="high",
    )
    defaults.update(overrides)
    return Event(**defaults)


@pytest.fixture
def bus():
    """EventBus with sweep disabled."""
    return EventBus(sweep_on_alert=False, sweep_interval=None)


@pytest_asyncio.fixture
async def store():
    """Async SQLite store with temp DB. Cleaned up after test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(db_path=path)
    await s.init()
    yield s
    await s.close()
    try:
        os.unlink(path)
    except OSError:
        pass
