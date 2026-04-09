"""Tests for reconcile.storage — async SQLite store."""

import asyncio
import pytest
from datetime import datetime, timezone, timedelta

from reconcile.schema import Event, Alert
from reconcile.analyzer import MemberProfile
from .conftest import event_factory


@pytest.mark.asyncio
async def test_init_creates_tables(store):
    cursor = await store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = {row[0] for row in await cursor.fetchall()}
    assert {"events", "alerts", "profiles", "teams", "metrics", "discord_servers"} <= tables


@pytest.mark.asyncio
async def test_event_has_hash_and_ingested_at(store):
    e = event_factory(team_id="t1", actor="bob", action="card.create")
    await store.append_event(e)
    await store.flush()
    events = await store.read_events("t1")
    assert events[0]["event_hash"] == e.event_hash
    assert events[0]["ingested_at"] is not None


@pytest.mark.asyncio
async def test_wal_mode(store):
    cursor = await store._db.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row[0] == "wal"


@pytest.mark.asyncio
async def test_event_round_trip(store):
    e = event_factory(team_id="t1", actor="bob", action="card.create")
    await store.append_event(e)
    await store.flush()
    events = await store.read_events("t1")
    assert len(events) == 1
    assert events[0]["actor"] == "bob"
    assert events[0]["action"] == "card.create"


@pytest.mark.asyncio
async def test_event_ordering_asc(store):
    now = datetime.now(timezone.utc)
    for i in range(3):
        await store.append_event(event_factory(
            team_id="t1", target=str(i),
            timestamp=now + timedelta(seconds=i),
        ))
    await store.flush()
    events = await store.read_events("t1")
    assert events[0]["target"] == "0"  # oldest first
    assert events[2]["target"] == "2"


@pytest.mark.asyncio
async def test_event_ordering_desc(store):
    now = datetime.now(timezone.utc)
    for i in range(3):
        await store.append_event(event_factory(
            team_id="t1", target=str(i),
            timestamp=now + timedelta(seconds=i),
        ))
    await store.flush()
    events = await store.read_events("t1", newest_first=True)
    assert events[0]["target"] == "2"  # newest first


@pytest.mark.asyncio
async def test_event_since_filter(store):
    now = datetime.now(timezone.utc)
    old = event_factory(team_id="t1", target="old", timestamp=now - timedelta(hours=1))
    new = event_factory(team_id="t1", target="new", timestamp=now)
    await store.append_event(old)
    await store.append_event(new)
    await store.flush()
    events = await store.read_events("t1", since=(now - timedelta(minutes=1)).isoformat())
    assert len(events) == 1
    assert events[0]["target"] == "new"


@pytest.mark.asyncio
async def test_duplicate_event_ignored(store):
    """Same event_hash = same logical event → deduped via UNIQUE constraint."""
    ts = datetime(2099, 1, 1, tzinfo=timezone.utc)
    e = event_factory(team_id="t1", timestamp=ts, source="test", actor="a", action="card.create", target="dup")
    await store.append_event(e)
    await store.append_event(e)  # exact same event → same hash
    await store.flush()
    events = await store.read_events("t1")
    dup_count = sum(1 for ev in events if ev["target"] == "dup")
    assert dup_count == 1


@pytest.mark.asyncio
async def test_different_metadata_different_hash(store):
    """Same action but different metadata → different hash → both stored."""
    ts = datetime(2099, 1, 2, tzinfo=timezone.utc)
    e1 = event_factory(team_id="t1", timestamp=ts, action="card.move", target="1", metadata={"to_pipeline": "A"})
    e2 = event_factory(team_id="t1", timestamp=ts, action="card.move", target="1", metadata={"to_pipeline": "B"})
    assert e1.event_hash != e2.event_hash
    await store.append_event(e1)
    await store.append_event(e2)
    await store.flush()
    events = await store.read_events("t1")
    assert len(events) >= 2


@pytest.mark.asyncio
async def test_alert_round_trip(store):
    a = Alert(detector="test", severity="elevated", title="Test", detail="Detail", team_id="t1")
    await store.append_alert(a)
    alerts = await store.read_alerts("t1")
    assert len(alerts) == 1
    assert alerts[0]["title"] == "Test"


@pytest.mark.asyncio
async def test_alert_severity_filter(store):
    await store.append_alert(Alert(detector="d", severity="info", title="a", detail="", team_id="t1"))
    await store.append_alert(Alert(detector="d", severity="critical", title="b", detail="", team_id="t1"))
    alerts = await store.read_alerts("t1", severity="critical")
    assert len(alerts) == 1
    assert alerts[0]["title"] == "b"


@pytest.mark.asyncio
async def test_alert_count(store):
    for i in range(3):
        await store.append_alert(Alert(detector="d", severity="info", title=str(i), detail="", team_id="t1"))
    assert await store.alert_count("t1") == 3
    assert await store.alert_count("t2") == 0


@pytest.mark.asyncio
async def test_severity_check_constraint(store):
    with pytest.raises(Exception):
        await store._db.execute(
            "INSERT INTO alerts (team_id, timestamp, detector, severity, title) VALUES (?, ?, ?, ?, ?)",
            ("t1", "2026-01-01", "d", "INVALID", "t"),
        )
        await store._db.commit()


@pytest.mark.asyncio
async def test_write_and_read_profiles(store):
    profiles = {
        "alice": MemberProfile(member="alice", direction="perpetrator", perpetrator_score=4, flags=[{"type": "test"}]),
    }
    await store.write_profiles("t1", profiles)
    result = await store.read_profiles("t1")
    assert len(result) == 1
    assert result[0]["member"] == "alice"
    assert result[0]["direction"] == "perpetrator"


@pytest.mark.asyncio
async def test_profile_versioning(store):
    """Each sweep appends a new version. Previous versions preserved."""
    p1 = {"alice": MemberProfile(member="alice", direction="neutral", perpetrator_score=0)}
    p2 = {"alice": MemberProfile(member="alice", direction="perpetrator", perpetrator_score=4)}
    await store.write_profiles("t1", p1)
    await store.write_profiles("t1", p2)
    # Latest
    latest = await store.read_profiles("t1")
    assert latest[0]["direction"] == "perpetrator"
    # Historical
    v1 = await store.read_profiles("t1", version=1)
    assert v1[0]["direction"] == "neutral"


@pytest.mark.asyncio
async def test_alert_links_to_event(store):
    """Alert should carry event_hash for audit trail."""
    a = Alert(detector="test", severity="elevated", title="t", detail="d", team_id="t1")
    await store.append_alert(a, event_hash="abc123")
    alerts = await store.read_alerts("t1")
    assert alerts[0]["event_hash"] == "abc123"


@pytest.mark.asyncio
async def test_save_team_preserves_created_at(store):
    await store.save_team("t1", "Alpha", {})
    teams = await store.list_teams()
    created = teams[0]["created_at"]

    await store.save_team("t1", "Alpha Updated", {"new": True})
    teams = await store.list_teams()
    assert teams[0]["created_at"] == created
    assert teams[0]["team_name"] == "Alpha Updated"


@pytest.mark.asyncio
async def test_list_teams_by_status(store):
    await store.save_team("t1", "A", {})
    await store.save_team("t2", "B", {})
    await store.archive_semester("")  # archives all with empty semester
    active = await store.list_teams(status="active")
    assert len(active) == 0


@pytest.mark.asyncio
async def test_cross_team_summary(store):
    await store.save_team("t1", "Alpha", {})
    await store.append_event(event_factory(team_id="t1"))
    await store.flush()
    await store.append_alert(Alert(detector="d", severity="info", title="a", detail="", team_id="t1"))
    summary = await store.cross_team_summary()
    assert len(summary) == 1
    assert summary[0]["event_count"] == 1
    assert summary[0]["alert_count"] == 1


@pytest.mark.asyncio
async def test_channel_event_write(store):
    """Events enqueued via channel should appear in DB after writer processes them."""
    await store.start_writer()
    e = event_factory(team_id="t1", actor="alice")
    store.enqueue_event(e)
    # Alert fence forces flush
    a = Alert(detector="d", severity="info", title="fence", detail="", team_id="t1")
    store.enqueue_alert(a)
    await asyncio.sleep(0.05)
    events = await store.read_events("t1")
    assert len(events) == 1
    assert events[0]["actor"] == "alice"
    alerts = await store.read_alerts("t1")
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_channel_alert_fence_flushes_events(store):
    """Alert durability fence should commit all pending events atomically."""
    await store.start_writer()
    for i in range(5):
        store.enqueue_event(event_factory(team_id="t1", actor=f"user{i}", target=str(i)))
    # No events committed yet (batch_size=500, only 5 enqueued)
    await asyncio.sleep(0.02)
    events_before = await store.read_events("t1")
    # Alert fence forces flush
    a = Alert(detector="d", severity="elevated", title="fence", detail="", team_id="t1")
    store.enqueue_alert(a)
    await asyncio.sleep(0.05)
    events_after = await store.read_events("t1")
    assert len(events_after) == 5


@pytest.mark.asyncio
async def test_channel_graceful_shutdown(store):
    """Shutdown should drain pending events."""
    await store.start_writer()
    for i in range(3):
        store.enqueue_event(event_factory(team_id="t1", target=str(i)))
    await store.close()
    # Reopen to verify persistence
    import aiosqlite
    db = await aiosqlite.connect(store.db_path)
    cursor = await db.execute("SELECT COUNT(*) FROM events WHERE team_id = 't1'")
    row = await cursor.fetchone()
    await db.close()
    assert row[0] == 3
