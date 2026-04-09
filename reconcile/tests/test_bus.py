"""Tests for reconcile.bus — EventBus priority queues, batch drain, sweep."""

import asyncio
import pytest
from unittest.mock import AsyncMock

from reconcile.bus import EventBus
from reconcile.schema import Alert
from .conftest import event_factory


@pytest.mark.asyncio
async def test_publish_high_priority(bus):
    e = event_factory(priority="high")
    await bus.publish(e)
    assert bus._high_queue.qsize() == 1
    assert bus._low_queue.qsize() == 0


@pytest.mark.asyncio
async def test_publish_low_priority(bus):
    e = event_factory(priority="low")
    await bus.publish(e)
    assert bus._low_queue.qsize() == 1
    assert bus._high_queue.qsize() == 0


@pytest.mark.asyncio
async def test_drain_batch_high_first(bus):
    low = event_factory(priority="low", action="session.join")
    high = event_factory(priority="high", action="card.move")
    await bus.publish(low)
    await bus.publish(high)
    batch = bus._drain_batch()
    assert len(batch) == 2
    assert batch[0].priority == "high"
    assert batch[1].priority == "low"


@pytest.mark.asyncio
async def test_drain_batch_respects_budget():
    bus = EventBus(sweep_on_alert=False, batch_size=2)
    for i in range(5):
        await bus.publish(event_factory(target=str(i)))
    batch = bus._drain_batch()
    assert len(batch) == 2


@pytest.mark.asyncio
async def test_wait_for_any_event_low(bus):
    bus._running = True
    low = event_factory(priority="low")
    await bus.publish(low)
    result = await bus._wait_for_any_event()
    assert len(result) == 1
    assert result[0].priority == "low"


@pytest.mark.asyncio
async def test_wait_for_any_event_timeout(bus):
    bus._running = True
    result = await bus._wait_for_any_event()
    assert result == []


@pytest.mark.asyncio
async def test_timeline_eviction():
    bus = EventBus(sweep_on_alert=False, timeline_max=5)
    bus._running = True
    processor = asyncio.create_task(bus._process_events())
    for i in range(10):
        await bus.publish(event_factory(target=str(i)))
    await asyncio.sleep(0.3)
    bus.stop()
    processor.cancel()
    try:
        await processor
    except asyncio.CancelledError:
        pass
    assert len(bus.timeline) <= 5


@pytest.mark.asyncio
async def test_subscribe_receives_alerts(bus):
    queue = asyncio.Queue()
    bus.subscribe_alerts(queue)

    class FakeDetector:
        name = "fake"
        async def detect(self, event):
            return [Alert(detector="fake", severity="info", title="t", detail="d")]

    bus.add_detector(FakeDetector())
    bus._running = True
    processor = asyncio.create_task(bus._process_events())
    await bus.publish(event_factory())
    await asyncio.sleep(0.3)
    bus.stop()
    processor.cancel()
    try:
        await processor
    except asyncio.CancelledError:
        pass
    assert not queue.empty()
    alert = queue.get_nowait()
    assert alert.detector == "fake"


@pytest.mark.asyncio
async def test_unsubscribe(bus):
    queue = asyncio.Queue()
    bus.subscribe_alerts(queue)
    bus.unsubscribe_alerts(queue)
    assert queue not in bus._alert_subscribers


@pytest.mark.asyncio
async def test_emit_alert_to_store(bus, store):
    bus.set_store(store)
    await store.start_writer()
    alert = Alert(detector="test", severity="elevated", title="t", detail="d", team_id="t1")
    await bus._emit_alert(alert)
    # Give writer a tick to process the durability fence
    await asyncio.sleep(0.05)
    alerts = await store.read_alerts("t1")
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_emit_alert_to_output(bus):
    mock_output = AsyncMock()
    bus.add_output(mock_output)
    alert = Alert(detector="test", severity="info", title="t", detail="d")
    await bus._emit_alert(alert)
    mock_output.emit.assert_awaited_once_with(alert)


@pytest.mark.asyncio
async def test_alert_counters_cqrs(bus):
    """AlertCounters should track severity and category counts."""
    a1 = Alert(detector="d", severity="critical", category="evidence", title="t", detail="d")
    a2 = Alert(detector="d", severity="elevated", category="attendance", title="t", detail="d")
    a3 = Alert(detector="d", severity="critical", category="evidence", title="t", detail="d")
    await bus._emit_alert(a1)
    await bus._emit_alert(a2)
    await bus._emit_alert(a3)
    snap = bus.alert_counters.snapshot()
    assert snap["total"] == 3
    assert snap["by_severity"]["critical"] == 2
    assert snap["by_severity"]["elevated"] == 1
    assert snap["by_category"]["evidence"] == 2
    assert snap["by_category"]["attendance"] == 1


def test_queue_depths(bus):
    assert bus.queue_depths == {"high": 0, "low": 0}
