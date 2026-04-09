"""Durability tests for reconcile storage — crash simulation, load stress, write channel integrity.

Tests from two perspectives:
  1. Systems engineering: crash recovery, signal handling, shutdown drain, data loss windows
  2. DB engineering: WAL integrity, atomic commits, dedup under load, concurrent read/write

Each test is independent. Store fixtures create isolated temp DBs.
"""

import asyncio
import os
import tempfile
import time

import aiosqlite
import pytest
import pytest_asyncio

from reconcile.schema import Event, Alert
from reconcile.analyzer import MemberProfile
from reconcile.bus import EventBus, AlertCounters
from reconcile.storage import Store, CHANNEL_SIZE, FLUSH_INTERVAL
from .conftest import event_factory


# ============================================================
# Fixtures
# ============================================================

@pytest_asyncio.fixture
async def store_with_writer():
    """Store with writer coroutine running. Cleaned up after test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(db_path=path, batch_size=50)  # small batch for faster tests
    await s.init()
    await s.start_writer()
    yield s
    await s.close()
    try:
        os.unlink(path)
        os.unlink(path + "-wal")
        os.unlink(path + "-shm")
    except OSError:
        pass


@pytest_asyncio.fixture
async def store_raw():
    """Store without writer (for direct write tests). Returns (store, path)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    s = Store(db_path=path)
    await s.init()
    yield s, path
    await s.close()
    try:
        os.unlink(path)
    except OSError:
        pass


# ============================================================
# 1. WRITE CHANNEL FUNDAMENTALS
# ============================================================

@pytest.mark.asyncio
class TestWriteChannel:
    """Core channel behavior: enqueue, flush, fence, shutdown."""

    @pytest.mark.asyncio
    async def test_event_enqueue_and_fence_flush(self, store_with_writer):
        """Events should persist after alert durability fence."""
        s = store_with_writer
        for i in range(10):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))
        s.enqueue_alert(Alert(detector="d", severity="info", title="fence", detail="", team_id="t1"))
        await asyncio.sleep(0.1)
        events = await s.read_events("t1")
        assert len(events) == 10

    @pytest.mark.asyncio
    async def test_events_not_visible_before_flush(self, store_with_writer):
        """Events below batch_size should NOT be in DB before flush trigger."""
        s = store_with_writer
        for i in range(5):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))
        await asyncio.sleep(0.05)
        # No fence, no batch threshold, no periodic flush yet
        events = await s.read_events("t1")
        assert len(events) == 0  # still in writer batch

    @pytest.mark.asyncio
    async def test_batch_size_triggers_flush(self, store_with_writer):
        """Hitting batch_size (50 in fixture) should auto-flush without fence."""
        s = store_with_writer
        for i in range(55):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))
        await asyncio.sleep(0.1)
        events = await s.read_events("t1")
        # At least 50 should have flushed (the batch), remaining 5 still in buffer
        assert len(events) >= 50

    @pytest.mark.asyncio
    async def test_multiple_fences_in_sequence(self, store_with_writer):
        """Multiple alert fences should each flush preceding events."""
        s = store_with_writer
        for round_num in range(3):
            for i in range(5):
                s.enqueue_event(event_factory(team_id="t1", target=f"{round_num}-{i}"))
            s.enqueue_alert(Alert(
                detector="d", severity="elevated", title=f"fence-{round_num}", detail="", team_id="t1",
            ))
        await asyncio.sleep(0.15)
        events = await s.read_events("t1", limit=100)
        alerts = await s.read_alerts("t1")
        assert len(events) == 15
        assert len(alerts) == 3

    @pytest.mark.asyncio
    async def test_fence_atomicity_event_alert_same_commit(self, store_with_writer):
        """Alert and its preceding events should be in the same transaction."""
        s = store_with_writer
        e = event_factory(team_id="t1", actor="alice", action="card.move")
        s.enqueue_event(e)
        a = Alert(detector="d", severity="critical", title="test", detail="", team_id="t1")
        s.enqueue_alert(a, event_hash=e.event_hash)
        await asyncio.sleep(0.1)
        # Both should be present
        events = await s.read_events("t1")
        alerts = await s.read_alerts("t1")
        assert len(events) == 1
        assert len(alerts) == 1
        # Alert should reference the event
        assert alerts[0]["event_hash"] == e.event_hash

    @pytest.mark.asyncio
    async def test_enqueue_without_writer_drops_silently(self, store_raw):
        """Enqueue without writer running should not crash (channel queues but nobody reads)."""
        s, _ = store_raw
        # Channel exists but no writer — items just sit in queue
        s.enqueue_event(event_factory(team_id="t1"))
        s.enqueue_alert(Alert(detector="d", severity="info", title="t", detail="", team_id="t1"))
        # No crash, no data in DB
        events = await s.read_events("t1")
        assert len(events) == 0


# ============================================================
# 2. GRACEFUL SHUTDOWN / DRAIN
# ============================================================

@pytest.mark.asyncio
class TestGracefulShutdown:
    """Verify shutdown drains all pending data before closing DB."""

    @pytest.mark.asyncio
    async def test_shutdown_drains_pending_events(self):
        """All enqueued events should persist after graceful close."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=500)
        await s.init()
        await s.start_writer()

        for i in range(100):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))

        await s.close()

        # Verify via fresh connection
        db = await aiosqlite.connect(path)
        cursor = await db.execute("SELECT COUNT(*) FROM events WHERE team_id = 't1'")
        count = (await cursor.fetchone())[0]
        await db.close()
        os.unlink(path)
        assert count == 100

    @pytest.mark.asyncio
    async def test_shutdown_drains_mixed_messages(self):
        """Shutdown should drain events, alerts, and profiles."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=500)
        await s.init()
        await s.start_writer()

        for i in range(20):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))
        s.enqueue_alert(Alert(detector="d", severity="info", title="a1", detail="", team_id="t1"))
        s.enqueue_profiles("t1", {
            "alice": MemberProfile(member="alice", direction="neutral"),
        })
        for i in range(20, 40):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))

        await s.close()

        db = await aiosqlite.connect(path)
        events = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        alerts = (await (await db.execute("SELECT COUNT(*) FROM alerts")).fetchone())[0]
        profiles = (await (await db.execute("SELECT COUNT(*) FROM profiles")).fetchone())[0]
        await db.close()
        os.unlink(path)

        assert events == 40
        assert alerts == 1
        assert profiles == 1

    @pytest.mark.asyncio
    async def test_shutdown_timeout_still_flushes_batch(self):
        """Even if writer task is slow, close() should attempt final safety flush."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=500)
        await s.init()
        # Don't start writer — simulate stuck writer
        # Directly put events in batch (simulating writer mid-batch)
        e = event_factory(team_id="t1")
        s._batch = [{
            "event_hash": e.event_hash,
            "team_id": "t1", "timestamp": "2026-01-01T00:00:00+00:00",
            "ingested_at": "2026-01-01T00:00:00+00:00",
            "source": "test", "actor": "alice", "action": "card.move",
            "target": "1", "target_type": "card", "metadata": "{}",
            "confidence": "server-authoritative", "priority": "high",
        }]
        await s.close()

        db = await aiosqlite.connect(path)
        count = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        await db.close()
        os.unlink(path)
        assert count == 1


# ============================================================
# 3. CRASH SIMULATION
# ============================================================

@pytest.mark.asyncio
class TestCrashSimulation:
    """Simulate ungraceful termination at various points in the write path."""

    @pytest.mark.asyncio
    async def test_crash_before_flush_loses_channel_events(self):
        """Events in channel (not yet batched) are lost on hard cancel. This is expected."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=500)
        await s.init()
        await s.start_writer()

        for i in range(10):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))

        # Simulate crash: cancel writer without drain
        s._writer_task.cancel()
        try:
            await s._writer_task
        except asyncio.CancelledError:
            pass

        # Events were in channel, never dequeued to batch → lost on hard cancel
        db = await aiosqlite.connect(path)
        count = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        await db.close()
        os.unlink(path)
        # Hard cancel loses unflushed channel items — this is the expected loss window
        assert count == 0

    @pytest.mark.asyncio
    async def test_crash_after_fence_preserves_fenced_data(self):
        """Data fenced by an alert should survive crash."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=500)
        await s.init()
        await s.start_writer()

        # Phase 1: fenced data
        for i in range(10):
            s.enqueue_event(event_factory(team_id="t1", target=f"fenced-{i}"))
        s.enqueue_alert(Alert(detector="d", severity="info", title="fence", detail="", team_id="t1"))
        await asyncio.sleep(0.1)

        # Phase 2: unfenced data
        for i in range(10):
            s.enqueue_event(event_factory(team_id="t1", target=f"unfenced-{i}"))

        # Simulate hard crash: kill writer, don't drain
        s._writer_task.cancel()
        try:
            await s._writer_task
        except asyncio.CancelledError:
            pass

        db = await aiosqlite.connect(path)
        events = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        alerts = (await (await db.execute("SELECT COUNT(*) FROM alerts")).fetchone())[0]
        await db.close()
        os.unlink(path)

        # Fenced events guaranteed, unfenced may or may not survive (best-effort)
        assert events >= 10  # at minimum the fenced ones
        assert alerts == 1

    @pytest.mark.asyncio
    async def test_crash_mid_batch_wal_integrity(self):
        """DB should be consistent after crash during batch write (WAL recovery)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=10)
        await s.init()
        await s.start_writer()

        # Enqueue enough to trigger multiple batch flushes
        for i in range(35):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))
        await asyncio.sleep(0.2)

        # Hard kill
        s._writer_task.cancel()
        try:
            await s._writer_task
        except asyncio.CancelledError:
            pass
        # Don't call close() — simulate ungraceful exit
        if s._db:
            await s._db.close()

        # Reopen and verify WAL recovery produces consistent state
        db = await aiosqlite.connect(path)
        await db.execute("PRAGMA journal_mode=WAL")
        cursor = await db.execute("SELECT COUNT(*) FROM events WHERE team_id = 't1'")
        count = (await cursor.fetchone())[0]
        # Verify no corruption: all rows should be readable
        cursor = await db.execute("SELECT event_hash, actor, action FROM events WHERE team_id = 't1'")
        rows = await cursor.fetchall()
        await db.close()
        os.unlink(path)

        # At least 30 should survive (3 full batches of 10)
        assert count >= 30
        assert len(rows) == count  # all rows readable, no corruption

    @pytest.mark.asyncio
    async def test_crash_recovery_reopens_cleanly(self):
        """After crash, a new Store instance should open the DB without errors."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        # Session 1: write some data, crash
        s1 = Store(db_path=path, batch_size=500)
        await s1.init()
        await s1.start_writer()
        for i in range(20):
            s1.enqueue_event(event_factory(team_id="t1", target=str(i)))
        s1.enqueue_alert(Alert(detector="d", severity="info", title="a", detail="", team_id="t1"))
        await asyncio.sleep(0.1)
        # Crash: just close DB
        if s1._db:
            await s1._db.close()

        # Session 2: reopen, should work cleanly
        s2 = Store(db_path=path)
        await s2.init()
        events = await s2.read_events("t1")
        alerts = await s2.read_alerts("t1")
        await s2.close()
        os.unlink(path)

        assert len(events) == 20
        assert len(alerts) == 1

    @pytest.mark.asyncio
    async def test_double_close_is_safe(self, store_with_writer):
        """Calling close() twice should not raise."""
        s = store_with_writer
        s.enqueue_event(event_factory(team_id="t1"))
        await s.close()
        # Second close should be safe (idempotent)
        await s.close()


# ============================================================
# 4. LOAD / STRESS TESTS
# ============================================================

@pytest.mark.asyncio
class TestLoadStress:
    """High-throughput and burst scenarios."""

    @pytest.mark.asyncio
    async def test_high_throughput_events(self):
        """10K events should all persist via channel."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=200)
        await s.init()
        await s.start_writer()

        n = 10_000
        for i in range(n):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))

        await s.close()

        db = await aiosqlite.connect(path)
        count = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        await db.close()
        os.unlink(path)
        assert count == n

    @pytest.mark.asyncio
    async def test_burst_with_interleaved_fences(self):
        """Burst of events with periodic fences should all persist."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=100)
        await s.init()
        await s.start_writer()

        total_events = 0
        total_alerts = 0
        for batch_num in range(20):
            for i in range(50):
                s.enqueue_event(event_factory(team_id="t1", target=f"{batch_num}-{i}"))
                total_events += 1
            s.enqueue_alert(Alert(
                detector="d", severity="info", title=f"fence-{batch_num}", detail="", team_id="t1",
            ))
            total_alerts += 1

        await s.close()

        db = await aiosqlite.connect(path)
        ev_count = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        al_count = (await (await db.execute("SELECT COUNT(*) FROM alerts")).fetchone())[0]
        await db.close()
        os.unlink(path)

        assert ev_count == total_events  # 1000
        assert al_count == total_alerts  # 20

    @pytest.mark.asyncio
    async def test_concurrent_reads_during_writes(self):
        """Reads should not block or corrupt during active writes (WAL)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=50)
        await s.init()
        await s.start_writer()

        read_counts = []

        async def reader():
            """Continuously read while writer is active."""
            for _ in range(20):
                events = await s.read_events("t1", limit=10000)
                read_counts.append(len(events))
                await asyncio.sleep(0.01)

        async def writer():
            for i in range(500):
                s.enqueue_event(event_factory(team_id="t1", target=str(i)))
                if i % 50 == 49:
                    s.enqueue_alert(Alert(
                        detector="d", severity="info", title=f"f{i}", detail="", team_id="t1",
                    ))
            await asyncio.sleep(0.2)

        await asyncio.gather(reader(), writer())
        await s.close()

        # Reads should show monotonically non-decreasing counts (WAL snapshot isolation)
        for i in range(1, len(read_counts)):
            assert read_counts[i] >= read_counts[i - 1], \
                f"Read count decreased: {read_counts[i-1]} -> {read_counts[i]} at index {i}"

        # All events should be persisted
        db = await aiosqlite.connect(path)
        final_count = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        await db.close()
        os.unlink(path)
        assert final_count == 500

    @pytest.mark.asyncio
    async def test_dedup_under_load(self):
        """Duplicate event_hashes should be silently ignored (INSERT OR IGNORE)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=50)
        await s.init()
        await s.start_writer()

        from datetime import datetime, timezone
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        # Same event 100 times — same event_hash
        for _ in range(100):
            s.enqueue_event(event_factory(
                team_id="t1", actor="alice", action="card.move", target="42", timestamp=ts,
            ))
        s.enqueue_alert(Alert(detector="d", severity="info", title="fence", detail="", team_id="t1"))
        await asyncio.sleep(0.1)
        await s.close()

        db = await aiosqlite.connect(path)
        count = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        await db.close()
        os.unlink(path)
        assert count == 1  # all dupes silently ignored

    @pytest.mark.asyncio
    async def test_multi_team_concurrent_writes(self):
        """Multiple teams writing concurrently should not interfere."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=50)
        await s.init()
        await s.start_writer()

        teams = ["team-a", "team-b", "team-c"]
        per_team = 200

        for tid in teams:
            for i in range(per_team):
                s.enqueue_event(event_factory(team_id=tid, target=f"{tid}-{i}"))
            s.enqueue_alert(Alert(detector="d", severity="info", title=f"fence-{tid}", detail="", team_id=tid))

        await s.close()

        db = await aiosqlite.connect(path)
        for tid in teams:
            cursor = await db.execute("SELECT COUNT(*) FROM events WHERE team_id = ?", (tid,))
            count = (await cursor.fetchone())[0]
            assert count == per_team, f"Team {tid} has {count} events, expected {per_team}"
        await db.close()
        os.unlink(path)

    @pytest.mark.asyncio
    async def test_stress_crash_during_high_throughput(self):
        """Crash during high-throughput burst: fenced data must survive."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=100)
        await s.init()
        await s.start_writer()

        # Phase 1: fenced burst
        fenced_count = 500
        for i in range(fenced_count):
            s.enqueue_event(event_factory(team_id="t1", target=f"fenced-{i}"))
        s.enqueue_alert(Alert(detector="d", severity="critical", title="fence", detail="", team_id="t1"))
        await asyncio.sleep(0.2)

        # Phase 2: unfenced burst (in-flight when crash happens)
        for i in range(500):
            s.enqueue_event(event_factory(team_id="t1", target=f"unfenced-{i}"))

        # Hard crash
        s._writer_task.cancel()
        try:
            await s._writer_task
        except asyncio.CancelledError:
            pass
        if s._db:
            await s._db.close()

        db = await aiosqlite.connect(path)
        total = (await (await db.execute("SELECT COUNT(*) FROM events")).fetchone())[0]
        alerts = (await (await db.execute("SELECT COUNT(*) FROM alerts")).fetchone())[0]
        await db.close()
        os.unlink(path)

        # Fenced data MUST survive
        assert total >= fenced_count
        assert alerts >= 1


# ============================================================
# 5. CQRS ALERT COUNTERS
# ============================================================

class TestAlertCounters:
    """Write-side projection correctness (sync tests, no marker needed)."""

    def test_counter_snapshot_initial(self):
        c = AlertCounters()
        snap = c.snapshot()
        assert snap["total"] == 0
        assert all(v == 0 for v in snap["by_severity"].values())
        assert all(v == 0 for v in snap["by_category"].values())

    def test_counter_record_and_snapshot(self):
        c = AlertCounters()
        c.record(Alert(detector="d", severity="critical", category="evidence", title="t", detail="d"))
        c.record(Alert(detector="d", severity="elevated", category="attendance", title="t", detail="d"))
        c.record(Alert(detector="d", severity="critical", category="evidence", title="t", detail="d"))
        snap = c.snapshot()
        assert snap["total"] == 3
        assert snap["by_severity"]["critical"] == 2
        assert snap["by_severity"]["elevated"] == 1
        assert snap["by_category"]["evidence"] == 2
        assert snap["by_category"]["attendance"] == 1

    def test_counter_unknown_severity_handled(self):
        """Unknown severity shouldn't crash, should be tracked."""
        c = AlertCounters()
        c.record(Alert(detector="d", severity="info", category="process", title="t", detail="d"))
        snap = c.snapshot()
        assert snap["by_severity"]["info"] == 1

    def test_snapshot_returns_copy(self):
        """Snapshot should be a copy, not a reference to internal state."""
        c = AlertCounters()
        snap1 = c.snapshot()
        c.record(Alert(detector="d", severity="info", category="process", title="t", detail="d"))
        snap2 = c.snapshot()
        assert snap1["total"] == 0
        assert snap2["total"] == 1


# ============================================================
# 6. WAL / DB ENGINEERING
# ============================================================

@pytest.mark.asyncio
class TestWALIntegrity:
    """SQLite WAL mode and synchronous=FULL correctness."""

    @pytest.mark.asyncio
    async def test_wal_mode_enabled(self, store_raw):
        """Store should be in WAL mode."""
        s, _ = store_raw
        cursor = await s._db.execute("PRAGMA journal_mode")
        mode = (await cursor.fetchone())[0]
        assert mode == "wal"

    @pytest.mark.asyncio
    async def test_synchronous_full(self, store_raw):
        """Store should use synchronous=FULL for crash safety."""
        s, _ = store_raw
        cursor = await s._db.execute("PRAGMA synchronous")
        val = (await cursor.fetchone())[0]
        # synchronous=FULL is value 2
        assert val == 2

    @pytest.mark.asyncio
    async def test_schema_check_constraints(self, store_raw):
        """CHECK constraints should reject invalid data."""
        s, _ = store_raw
        # Invalid severity
        with pytest.raises(Exception):
            await s._db.execute(
                "INSERT INTO alerts (team_id, timestamp, detector, severity, title) VALUES (?, ?, ?, ?, ?)",
                ("t1", "2026-01-01", "d", "INVALID_SEV", "t"),
            )
            await s._db.commit()

        # Invalid category
        with pytest.raises(Exception):
            await s._db.execute(
                "INSERT INTO alerts (team_id, timestamp, detector, severity, category, title) VALUES (?, ?, ?, ?, ?, ?)",
                ("t1", "2026-01-01", "d", "info", "INVALID_CAT", "t"),
            )
            await s._db.commit()

    @pytest.mark.asyncio
    async def test_event_hash_uniqueness(self, store_raw):
        """Duplicate event_hash should be silently ignored (INSERT OR IGNORE)."""
        s, _ = store_raw
        e = event_factory(team_id="t1")
        await s.append_event(e)
        await s.append_event(e)  # same event_hash
        events = await s.read_events("t1")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_profile_versioning_atomicity(self):
        """Profile version should be atomic — all members or none."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path)
        await s.init()
        await s.start_writer()

        profiles = {
            "alice": MemberProfile(member="alice", direction="perpetrator", perpetrator_score=4, flags=[]),
            "bob": MemberProfile(member="bob", direction="victim", victim_score=2, flags=[]),
            "carol": MemberProfile(member="carol", direction="neutral", flags=[]),
        }
        s.enqueue_profiles("t1", profiles)
        await asyncio.sleep(0.1)

        result = await s.read_profiles("t1")
        await s.close()
        os.unlink(path)

        assert len(result) == 3
        # All should be same version
        versions = {r["version"] for r in result}
        assert len(versions) == 1

    @pytest.mark.asyncio
    async def test_profile_version_increments(self, store_with_writer):
        """Each sweep should increment the version number."""
        s = store_with_writer
        p1 = {"alice": MemberProfile(member="alice", direction="neutral", flags=[])}
        p2 = {"alice": MemberProfile(member="alice", direction="perpetrator", perpetrator_score=4, flags=[])}

        s.enqueue_profiles("t1", p1)
        await asyncio.sleep(0.1)
        s.enqueue_profiles("t1", p2)
        await asyncio.sleep(0.1)

        # Latest version
        latest = await s.read_profiles("t1")
        assert len(latest) == 1
        assert latest[0]["direction"] == "perpetrator"

        # Version 1 still accessible
        v1 = await s.read_profiles("t1", version=1)
        assert len(v1) == 1
        assert v1[0]["direction"] == "neutral"


# ============================================================
# 7. CHANNEL CAPACITY / BACKPRESSURE
# ============================================================

@pytest.mark.asyncio
class TestChannelCapacity:
    """Bounded channel behavior under pressure."""

    @pytest.mark.asyncio
    async def test_channel_size_is_bounded(self, store_raw):
        """Channel should have the expected max size."""
        s, _ = store_raw
        assert s._channel.maxsize == CHANNEL_SIZE

    @pytest.mark.asyncio
    async def test_channel_full_drops_events(self):
        """When channel is full, enqueue should drop and log (not block)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path)
        await s.init()
        # Don't start writer — channel fills up
        # Use a tiny channel for testing
        s._channel = asyncio.Queue(maxsize=5)

        for i in range(10):
            s.enqueue_event(event_factory(team_id="t1", target=str(i)))

        # Channel should have exactly 5 items (5 dropped)
        assert s._channel.qsize() == 5
        await s.close()
        os.unlink(path)

    @pytest.mark.asyncio
    async def test_channel_full_drops_alert_logged(self):
        """Alert drops on full channel should be logged (critical data loss)."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path)
        await s.init()
        s._channel = asyncio.Queue(maxsize=2)

        s.enqueue_event(event_factory(team_id="t1"))
        s.enqueue_event(event_factory(team_id="t1"))
        # Channel now full
        s.enqueue_alert(Alert(detector="d", severity="critical", title="dropped!", detail="", team_id="t1"))
        # Should not raise, just log error
        assert s._channel.qsize() == 2  # alert didn't fit
        await s.close()
        os.unlink(path)


# ============================================================
# 8. E2E: BUS + STORE INTEGRATION
# ============================================================

@pytest.mark.asyncio
class TestE2EBusStore:
    """End-to-end: events flow through bus → detectors → alerts → store."""

    @pytest.mark.asyncio
    async def test_bus_event_to_store_via_channel(self):
        """Event published to bus should end up in store via channel."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=50)
        await s.init()
        await s.start_writer()

        bus = EventBus(sweep_on_alert=False, sweep_interval=None)
        bus.set_store(s)

        e = event_factory(team_id="t1", actor="alice")
        await bus.publish(e)

        # Drain the bus manually
        batch = bus._drain_batch()
        for event in batch:
            s.enqueue_event(event)

        # Fence to flush
        s.enqueue_alert(Alert(detector="d", severity="info", title="fence", detail="", team_id="t1"))
        await asyncio.sleep(0.1)

        events = await s.read_events("t1")
        await s.close()
        os.unlink(path)
        assert len(events) == 1
        assert events[0]["actor"] == "alice"

    @pytest.mark.asyncio
    async def test_alert_counters_match_store(self):
        """CQRS counters should match DB alert counts after flush."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        s = Store(db_path=path, batch_size=50)
        await s.init()
        await s.start_writer()

        bus = EventBus(sweep_on_alert=False, sweep_interval=None)
        bus.set_store(s)

        for sev in ["critical", "suspect", "elevated", "info", "critical"]:
            a = Alert(detector="d", severity=sev, category="process", title=f"a-{sev}", detail="", team_id="t1")
            await bus._emit_alert(a)

        await asyncio.sleep(0.1)

        # Counters
        snap = bus.alert_counters.snapshot()
        assert snap["total"] == 5
        assert snap["by_severity"]["critical"] == 2

        # DB
        db_count = await s.alert_count("t1")
        assert db_count == snap["total"]

        await s.close()
        os.unlink(path)
