"""Async SQLite store with single-writer channel.

Write path: enqueue_event/enqueue_alert/enqueue_profiles → bounded asyncio.Queue → single writer coroutine → DB
Read path:  read_* methods go direct to DB (CQRS separation)

Alerts act as durability fences — flush all pending events + commit alert atomically.
Periodic flush (5s) covers quiet periods. Bounded queue (50K) caps memory.

Dependencies: pip install aiosqlite
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    event_hash TEXT NOT NULL UNIQUE,
    team_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    source TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    metadata JSON,
    confidence TEXT NOT NULL DEFAULT 'server-authoritative',
    priority TEXT NOT NULL DEFAULT 'high'
);
CREATE INDEX IF NOT EXISTS idx_events_team ON events(team_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_action ON events(action, team_id);
-- event_hash UNIQUE constraint already creates an implicit index; no explicit idx needed

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    detector TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info', 'elevated', 'suspect', 'critical')),
    category TEXT NOT NULL DEFAULT 'process' CHECK(category IN ('process', 'attendance', 'attribution', 'evidence')),
    score INTEGER NOT NULL DEFAULT 2,
    title TEXT NOT NULL,
    detail TEXT,
    metadata JSON,
    event_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_team ON alerts(team_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(team_id, severity, timestamp);
CREATE INDEX IF NOT EXISTS idx_alerts_event ON alerts(event_hash);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    member TEXT NOT NULL,
    direction TEXT,
    perpetrator_score INTEGER DEFAULT 0,
    victim_score INTEGER DEFAULT 0,
    flags JSON,
    commits INTEGER DEFAULT 0,
    messages_sent INTEGER DEFAULT 0,
    cards_completed INTEGER DEFAULT 0,
    cards_completed_zero_commits INTEGER DEFAULT 0,
    branches_deleted INTEGER DEFAULT 0,
    files_reattributed_to INTEGER DEFAULT 0,
    files_reattributed_from INTEGER DEFAULT 0,
    proactive_count INTEGER DEFAULT 0,
    meetings_present INTEGER DEFAULT 0,
    meetings_absent INTEGER DEFAULT 0,
    swept_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    profile_hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_profiles_team ON profiles(team_id, member, version);

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    team_name TEXT,
    semester TEXT,
    config JSON NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'stopped', 'archived')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    events_total INTEGER,
    events_per_min REAL,
    alerts_total INTEGER,
    ingestor_status JSON,
    detector_status JSON
);
CREATE INDEX IF NOT EXISTS idx_metrics_team ON metrics(team_id, timestamp);

CREATE TABLE IF NOT EXISTS discord_servers (
    guild_id TEXT PRIMARY KEY,
    team_id TEXT NOT NULL,
    invite_url TEXT,
    channel_ids JSON,
    role_ids JSON,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived', 'deleted')),
    created_at TEXT NOT NULL,
    archived_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_discord_team ON discord_servers(team_id);

CREATE TABLE IF NOT EXISTS branch_resolutions (
    team_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    resolved_author TEXT,
    resolution_method TEXT NOT NULL,
    evidence_quality TEXT NOT NULL,
    signals JSON NOT NULL,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (team_id, branch)
);

CREATE TABLE IF NOT EXISTS system_logs (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL DEFAULT '',
    timestamp REAL NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    msg TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON system_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_logs_team ON system_logs(team_id, timestamp);

-- Sprint boundaries (PM-authoritative from status report dates)
CREATE TABLE IF NOT EXISTS sprint_windows (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    sprint_number INTEGER NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'status-report',
    UNIQUE(team_id, sprint_number)
);

-- Per-sprint collaboration metric snapshots (CQRS read model)
CREATE TABLE IF NOT EXISTS collaboration_snapshots (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    sprint_id INTEGER NOT NULL,
    computed_at TEXT NOT NULL,
    gini REAL,
    entropy_norm REAL,
    interaction_density REAL,
    bus_factor INTEGER,
    clustering_ratio REAL,
    cadence_regularity REAL,
    churn_balance REAL,
    attendance_corr REAL,
    health_score REAL,
    per_member JSON,
    interaction_graph JSON,
    lead_time_detail JSON,
    cadence_detail JSON,
    UNIQUE(team_id, sprint_id)
);

-- Git blame/churn cache keyed by HEAD SHA
CREATE TABLE IF NOT EXISTS git_cache (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    computed_at TEXT NOT NULL,
    data JSON NOT NULL,
    UNIQUE(team_id, cache_key)
);
"""

# SQL for batch event insert
_EVENT_INSERT_SQL = (
    "INSERT OR IGNORE INTO events "
    "(event_hash, team_id, timestamp, ingested_at, source, actor, action, target, target_type, metadata, confidence, priority) "
    "VALUES (:event_hash, :team_id, :timestamp, :ingested_at, :source, :actor, :action, :target, :target_type, :metadata, :confidence, :priority)"
)

_ALERT_INSERT_SQL = (
    "INSERT INTO alerts "
    "(team_id, timestamp, detector, severity, category, score, title, detail, metadata, event_hash) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_PROFILE_INSERT_SQL = (
    "INSERT INTO profiles "
    "(team_id, member, direction, perpetrator_score, victim_score, flags, commits, messages_sent, "
    "cards_completed, cards_completed_zero_commits, branches_deleted, files_reattributed_to, "
    "files_reattributed_from, proactive_count, meetings_present, meetings_absent, swept_at, version, profile_hash) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_LOG_INSERT_SQL = (
    "INSERT INTO system_logs (team_id, timestamp, level, source, msg) "
    "VALUES (?, ?, ?, ?, ?)"
)

CHANNEL_SIZE = 50_000
FLUSH_INTERVAL = 5.0
LOG_RETENTION_DAYS = 7


class Store:
    """Single-writer async SQLite store.

    Write path: bounded channel → single writer coroutine → DB.
    Read path: direct DB queries (concurrent with writes via WAL).
    Alerts act as durability fences.
    """

    def __init__(self, db_path: str = "data/reconcile.db", batch_size: int = 500):
        self.db_path = db_path
        self.batch_size = batch_size
        self._db = None
        self._channel: asyncio.Queue | None = None
        self._writer_task: asyncio.Task | None = None
        self._batch: list[dict] = []
        self._log_batch: list[tuple] = []

    async def init(self) -> None:
        """Create tables, enable WAL mode, init channel. Call once on startup."""
        import aiosqlite

        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=FULL")
        await self._db.execute("PRAGMA cache_size=-20000")  # 20MB page cache
        await self._db.execute("PRAGMA mmap_size=268435456")  # 256MB memory-mapped I/O
        await self._db.executescript(SCHEMA_SQL)
        await self._db.commit()
        # Migrate existing DBs: add missing columns (idempotent — silently skips if exists)
        migrations = [
            ("alerts", "category", "TEXT NOT NULL DEFAULT 'process'"),
            ("alerts", "score", "INTEGER NOT NULL DEFAULT 2"),
            ("profiles", "cards_completed", "INTEGER DEFAULT 0"),
            ("profiles", "cards_completed_zero_commits", "INTEGER DEFAULT 0"),
            ("profiles", "branches_deleted", "INTEGER DEFAULT 0"),
            ("profiles", "files_reattributed_to", "INTEGER DEFAULT 0"),
            ("profiles", "files_reattributed_from", "INTEGER DEFAULT 0"),
            ("profiles", "proactive_count", "INTEGER DEFAULT 0"),
            ("profiles", "meetings_present", "INTEGER DEFAULT 0"),
            ("profiles", "meetings_absent", "INTEGER DEFAULT 0"),
            ("profiles", "profile_hash", "TEXT DEFAULT ''"),
        ]
        for table, col, col_type in migrations:
            try:
                await self._db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
                await self._db.commit()
            except Exception:
                pass  # column already exists
        # Rotate old system logs on startup
        cutoff = time.time() - (LOG_RETENTION_DAYS * 86400)
        try:
            cursor = await self._db.execute("DELETE FROM system_logs WHERE timestamp < ?", (cutoff,))
            if cursor.rowcount:
                log.info("Rotated %d system log entries older than %d days", cursor.rowcount, LOG_RETENTION_DAYS)
            await self._db.commit()
        except Exception:
            pass  # table may not exist yet on first run (schema just created)
        self._channel = asyncio.Queue(maxsize=CHANNEL_SIZE)
        log.info("Store initialized: %s (WAL, synchronous=FULL, channel=%d)", self.db_path, CHANNEL_SIZE)

    async def start_writer(self) -> None:
        """Start the single writer coroutine. Call after init()."""
        self._writer_task = asyncio.create_task(self._writer_loop(), name="store-writer")
        self._writer_task.add_done_callback(self._on_writer_done)

    def _on_writer_done(self, task: asyncio.Task) -> None:
        """Watchdog: log CRITICAL if writer dies unexpectedly."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.critical("Store writer DIED: %s — persistence halted!", exc, exc_info=exc)

    # ========================================================
    # Writer coroutine (single consumer, owns all DB writes)
    # ========================================================

    async def _writer_loop(self) -> None:
        """Single consumer: batches events, alerts act as durability fences."""
        last_flush = time.monotonic()
        while True:
            try:
                timeout = max(0.1, FLUSH_INTERVAL - (time.monotonic() - last_flush))
                try:
                    msg = await asyncio.wait_for(self._channel.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    if self._batch:
                        await self._flush_batch()
                        last_flush = time.monotonic()
                    continue

                msg_type, data = msg

                if msg_type == "event":
                    self._batch.append(data)
                    if len(self._batch) >= self.batch_size:
                        await self._flush_batch()
                        last_flush = time.monotonic()

                elif msg_type == "log":
                    self._log_batch.append((
                        data.get("team_id", ""), data.get("ts", time.time()),
                        data.get("level", "info"), data.get("source", ""),
                        data.get("msg", ""),
                    ))
                    # Logs flush with the next event flush (same 5s timer)

                elif msg_type == "alert_fence":
                    await self._flush_with_alert(data)
                    last_flush = time.monotonic()

                elif msg_type == "profiles":
                    if len(data) == 3:
                        team_id, profiles_data, phash = data
                    else:
                        team_id, profiles_data = data
                        phash = ""
                    await self._write_profiles_atomic(team_id, profiles_data, profile_hash=phash)

                elif msg_type == "shutdown":
                    if self._batch:
                        await self._flush_batch()
                    return

            except asyncio.CancelledError:
                if self._batch:
                    try:
                        await self._flush_batch()
                    except Exception:
                        pass
                return
            except Exception as e:
                log.error("Writer error: %s", e, exc_info=True)

    async def _flush_batch(self) -> None:
        """Flush pending event + log batches to DB in a single commit."""
        if not self._db:
            return
        if not self._batch and not self._log_batch:
            return
        event_batch = self._batch
        log_batch = self._log_batch
        self._batch = []
        self._log_batch = []
        if event_batch:
            await self._db.executemany(_EVENT_INSERT_SQL, event_batch)
        if log_batch:
            await self._db.executemany(_LOG_INSERT_SQL, log_batch)
        await self._db.commit()
        if event_batch or log_batch:
            log.debug("Flushed %d events + %d logs to SQLite", len(event_batch), len(log_batch))

    async def _flush_with_alert(self, alert_data: tuple) -> None:
        """Durability fence: commit pending events + alert atomically."""
        if not self._db:
            return
        if self._batch:
            batch = self._batch
            self._batch = []
            await self._db.executemany(_EVENT_INSERT_SQL, batch)
        await self._db.execute(_ALERT_INSERT_SQL, alert_data)
        await self._db.commit()

    async def _write_profiles_atomic(self, team_id: str, profiles: dict, profile_hash: str = "") -> None:
        """Atomic profile snapshot write via channel."""
        if not self._db:
            return
        cursor = await self._db.execute(
            "SELECT COALESCE(MAX(version), 0) FROM profiles WHERE team_id = ?", (team_id,),
        )
        row = await cursor.fetchone()
        next_version = (row[0] if row else 0) + 1
        now = datetime.now(timezone.utc).isoformat()
        for member, p in profiles.items():
            await self._db.execute(
                _PROFILE_INSERT_SQL,
                (
                    team_id, member, p.direction,
                    p.perpetrator_score, p.victim_score,
                    json.dumps(p.flags), p.commits, p.messages_sent,
                    p.cards_completed, p.cards_completed_zero_commits,
                    p.branches_deleted, p.files_reattributed_to,
                    p.files_reattributed_from, p.proactive_count,
                    p.meetings_present, p.meetings_absent,
                    now, next_version, profile_hash,
                ),
            )
        await self._db.commit()
        log.debug("Wrote profile snapshot v%d for team %s (%d members)", next_version, team_id, len(profiles))

    # ========================================================
    # Public write API (enqueue, non-blocking)
    # ========================================================

    def enqueue_event(self, event) -> None:
        """Non-blocking enqueue. Drops if channel full."""
        if not self._channel:
            return
        data = {
            "event_hash": event.event_hash,
            "team_id": event.team_id,
            "timestamp": event.timestamp.isoformat(),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
            "source": event.source,
            "actor": event.actor,
            "action": event.action,
            "target": event.target,
            "target_type": event.target_type,
            "metadata": json.dumps(event.metadata),
            "confidence": event.confidence,
            "priority": event.priority,
        }
        try:
            self._channel.put_nowait(("event", data))
        except asyncio.QueueFull:
            log.warning("Write channel full, dropping event %s", event.event_hash)

    def enqueue_alert(self, alert, event_hash: str = "") -> None:
        """Non-blocking enqueue. Alert = durability fence."""
        if not self._channel:
            return
        data = (
            alert.team_id, alert.timestamp.isoformat(), alert.detector,
            alert.severity, alert.category, alert.score,
            alert.title, alert.detail, json.dumps(alert.metadata), event_hash,
        )
        try:
            self._channel.put_nowait(("alert_fence", data))
        except asyncio.QueueFull:
            log.error("Write channel full, DROPPING ALERT %s", alert.title)

    def enqueue_profiles(self, team_id: str, profiles: dict, profile_hash: str = "") -> None:
        """Non-blocking enqueue for profile snapshot."""
        if not self._channel:
            return
        try:
            self._channel.put_nowait(("profiles", (team_id, profiles, profile_hash)))
        except asyncio.QueueFull:
            log.error("Write channel full, dropping profiles for %s", team_id)

    def enqueue_log(self, entry: dict) -> None:
        """Non-blocking enqueue for system log entry."""
        if not self._channel:
            return
        try:
            self._channel.put_nowait(("log", entry))
        except asyncio.QueueFull:
            pass  # logs are best-effort

    # Legacy write methods (kept for direct writes in tests/sweep without channel)

    async def append_event(self, event) -> None:
        """Direct event write (bypasses channel). Used when writer not running."""
        if not self._db:
            return
        await self._db.execute(
            _EVENT_INSERT_SQL,
            {
                "event_hash": event.event_hash,
                "team_id": event.team_id,
                "timestamp": event.timestamp.isoformat(),
                "ingested_at": datetime.now(timezone.utc).isoformat(),
                "source": event.source,
                "actor": event.actor,
                "action": event.action,
                "target": event.target,
                "target_type": event.target_type,
                "metadata": json.dumps(event.metadata),
                "confidence": event.confidence,
                "priority": event.priority,
            },
        )
        await self._db.commit()

    async def append_alert(self, alert, event_hash: str = "") -> None:
        """Direct alert write (bypasses channel). Used when writer not running."""
        if not self._db:
            return
        await self._db.execute(
            _ALERT_INSERT_SQL,
            (
                alert.team_id, alert.timestamp.isoformat(), alert.detector,
                alert.severity, alert.category, alert.score,
                alert.title, alert.detail, json.dumps(alert.metadata), event_hash,
            ),
        )
        await self._db.commit()

    async def write_profiles(self, team_id: str, profiles: dict) -> None:
        """Direct profile write (bypasses channel). Used when writer not running."""
        await self._write_profiles_atomic(team_id, profiles)

    async def flush(self) -> None:
        """Flush writer batch. For tests and shutdown."""
        if self._batch:
            await self._flush_batch()

    # ========================================================
    # Read API (direct DB, concurrent with writes via WAL)
    # ========================================================

    # Column projections — avoid fetching large JSON blobs for list views
    _EVENT_LIST_COLS = "id, event_hash, team_id, timestamp, ingested_at, source, actor, action, target, target_type, confidence, priority"
    _ALERT_LIST_COLS = "id, team_id, timestamp, detector, severity, category, score, title, detail, event_hash"

    async def read_logs(
        self, limit: int = 200, team_id: str | None = None, level: str | None = None,
    ) -> list[dict]:
        """Read recent system log entries."""
        if not self._db:
            return []
        conditions = []
        params: list = []
        if team_id:
            conditions.append("team_id = ?")
            params.append(team_id)
        if level:
            conditions.append("level = ?")
            params.append(level)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT id, team_id, timestamp as ts, level, source, msg FROM system_logs {where} ORDER BY id DESC LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()
        if not rows or not cursor.description:
            return []
        cols = [d[0] for d in cursor.description]
        # Return in chronological order (oldest first) for SSE backfill
        return [dict(zip(cols, row)) for row in reversed(rows)]

    async def read_events(
        self, team_id: str, since: str | None = None, before: str | None = None,
        limit: int = 1000, newest_first: bool = False,
    ) -> list[dict]:
        if not self._db:
            return []
        order = "DESC" if newest_first else "ASC"
        assert order in ("ASC", "DESC"), f"Invalid order: {order}"
        cols_sql = self._EVENT_LIST_COLS
        conditions = ["team_id = ?"]
        params: list = [team_id]
        if since:
            conditions.append("timestamp > ?")
            params.append(since)
        if before:
            conditions.append("timestamp < ?")
            params.append(before)
        where = " AND ".join(conditions)
        params.append(limit)
        cursor = await self._db.execute(
            f"SELECT {cols_sql} FROM events WHERE {where} ORDER BY timestamp {order} LIMIT ?",
            tuple(params),
        )
        rows = await cursor.fetchall()
        if not cursor.description:
            return []
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def read_alerts(
        self, team_id: str | None = None, limit: int = 100, severity: str | None = None
    ) -> list[dict]:
        if not self._db:
            return []
        conditions = []
        params: list = []
        if team_id:
            conditions.append("team_id = ?")
            params.append(team_id)
        if severity:
            conditions.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = await self._db.execute(
            f"SELECT {self._ALERT_LIST_COLS} FROM alerts {where} ORDER BY timestamp DESC LIMIT ?",
            params + [limit],
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def alert_count(self, team_id: str | None = None) -> int:
        if not self._db:
            return 0
        if team_id:
            cursor = await self._db.execute("SELECT COUNT(*) FROM alerts WHERE team_id = ?", (team_id,))
        else:
            cursor = await self._db.execute("SELECT COUNT(*) FROM alerts")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def alert_counts_by_team(self) -> dict[str, int]:
        """Single GROUP BY instead of N separate COUNT queries."""
        if not self._db:
            return {}
        cursor = await self._db.execute("SELECT team_id, COUNT(*) FROM alerts GROUP BY team_id")
        return {row[0]: row[1] for row in await cursor.fetchall()}

    async def hydrate_alert_counters(self, team_id: str) -> list[tuple]:
        """Load alert counts for one team, grouped by severity+category.

        Returns [(severity, category, count), ...] for AlertCounters.hydrate().
        Max 16 rows (4 severities × 4 categories). Single indexed scan.
        """
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT severity, category, COUNT(*) FROM alerts WHERE team_id = ? GROUP BY severity, category",
            (team_id,),
        )
        return await cursor.fetchall()

    # --- Branch resolutions ---

    async def upsert_resolution(self, team_id: str, branch: str, resolved_author: str | None,
                                 method: str, quality: str, signals: dict) -> None:
        """Insert or update a branch resolution. Returns previous resolution if changed."""
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO branch_resolutions (team_id, branch, resolved_author, resolution_method, evidence_quality, signals, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(team_id, branch) DO UPDATE SET "
            "resolved_author=excluded.resolved_author, resolution_method=excluded.resolution_method, "
            "evidence_quality=excluded.evidence_quality, signals=excluded.signals, resolved_at=excluded.resolved_at",
            (team_id, branch, resolved_author, method, quality, json.dumps(signals), now),
        )
        await self._db.commit()

    async def read_resolution(self, team_id: str, branch: str) -> dict | None:
        """Read a single branch resolution."""
        if not self._db:
            return None
        cursor = await self._db.execute(
            "SELECT * FROM branch_resolutions WHERE team_id = ? AND branch = ?",
            (team_id, branch),
        )
        row = await cursor.fetchone()
        if not row or not cursor.description:
            return None
        cols = [d[0] for d in cursor.description]
        return dict(zip(cols, row))

    async def read_resolutions(self, team_id: str) -> list[dict]:
        """Read all branch resolutions for a team."""
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT * FROM branch_resolutions WHERE team_id = ? ORDER BY branch",
            (team_id,),
        )
        rows = await cursor.fetchall()
        if not rows or not cursor.description:
            return []
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def read_profiles(self, team_id: str, version: int | None = None) -> list[dict]:
        if not self._db:
            return []
        if version is not None:
            # Explicit version — direct query
            cursor = await self._db.execute(
                "SELECT * FROM profiles WHERE team_id = ? AND version = ? ORDER BY perpetrator_score DESC",
                (team_id, version),
            )
        else:
            # Latest version — single query with subquery (atomic, no race)
            cursor = await self._db.execute(
                "SELECT * FROM profiles WHERE team_id = ? AND version = "
                "(SELECT COALESCE(MAX(version), 0) FROM profiles WHERE team_id = ?) "
                "ORDER BY perpetrator_score DESC",
                (team_id, team_id),
            )
        rows = await cursor.fetchall()
        if not rows:
            return []
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Teams ---

    async def save_team(self, team_id: str, team_name: str, config: dict, semester: str = "") -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT OR IGNORE INTO teams (team_id, team_name, semester, config, status, created_at) "
            "VALUES (?, ?, ?, ?, 'active', ?)",
            (team_id, team_name, semester, json.dumps(config), now),
        )
        await self._db.execute(
            "UPDATE teams SET team_name = ?, semester = ?, config = ? WHERE team_id = ?",
            (team_name, semester, json.dumps(config), team_id),
        )
        await self._db.commit()

    async def list_teams(self, status: str | None = None) -> list[dict]:
        if not self._db:
            return []
        if status:
            cursor = await self._db.execute(
                "SELECT * FROM teams WHERE status = ? ORDER BY team_name", (status,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM teams ORDER BY team_name")
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def archive_semester(self, semester: str) -> int:
        if not self._db:
            return 0
        cursor = await self._db.execute(
            "UPDATE teams SET status = 'archived' WHERE semester = ?", (semester,)
        )
        await self._db.commit()
        return cursor.rowcount

    # --- Metrics ---

    async def write_metrics(self, team_id: str, metrics: dict) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO metrics (team_id, timestamp, events_total, events_per_min, alerts_total, ingestor_status, detector_status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                team_id, now,
                metrics.get("events_total", 0),
                metrics.get("events_per_min", 0.0),
                metrics.get("alerts_total", 0),
                json.dumps(metrics.get("ingestor_status", {})),
                json.dumps(metrics.get("detector_status", {})),
            ),
        )
        await self._db.commit()

    async def get_metrics(self, team_id: str, last_n: int = 60) -> list[dict]:
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT * FROM metrics WHERE team_id = ? ORDER BY timestamp DESC LIMIT ?",
            (team_id, last_n),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Cross-team ---

    async def cross_team_summary(self) -> list[dict]:
        """Aggregated summary per active team. JOIN-based — O(1) scans, not O(N) subqueries."""
        if not self._db:
            return []
        cursor = await self._db.execute("""
            SELECT
                t.team_id, t.team_name, t.status,
                COALESCE(ec.cnt, 0) as event_count,
                COALESCE(ac.cnt, 0) as alert_count,
                ac.last_alert
            FROM teams t
            LEFT JOIN (SELECT team_id, COUNT(*) as cnt FROM events GROUP BY team_id) ec
                ON ec.team_id = t.team_id
            LEFT JOIN (SELECT team_id, COUNT(*) as cnt, MAX(timestamp) as last_alert FROM alerts GROUP BY team_id) ac
                ON ac.team_id = t.team_id
            WHERE t.status = 'active'
            ORDER BY alert_count DESC
        """)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Discord servers ---

    async def save_discord_server(self, result) -> None:
        if not self._db:
            return
        await self._db.execute(
            "INSERT OR REPLACE INTO discord_servers (guild_id, team_id, invite_url, channel_ids, role_ids, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?)",
            (
                result.guild_id, result.team_id, result.invite_url,
                json.dumps(result.channel_ids), json.dumps(result.role_ids),
                result.created_at,
            ),
        )
        await self._db.commit()

    async def list_discord_servers(self, team_id: str | None = None) -> list[dict]:
        if not self._db:
            return []
        if team_id:
            cursor = await self._db.execute(
                "SELECT * FROM discord_servers WHERE team_id = ?", (team_id,)
            )
        else:
            cursor = await self._db.execute("SELECT * FROM discord_servers ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Sprint windows ---

    async def upsert_sprint_window(self, team_id: str, sprint_number: int,
                                   start_date: str, end_date: str,
                                   source: str = "status-report") -> None:
        if not self._db:
            return
        await self._db.execute(
            "INSERT OR REPLACE INTO sprint_windows (team_id, sprint_number, start_date, end_date, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (team_id, sprint_number, start_date, end_date, source),
        )
        await self._db.commit()

    async def read_sprint_windows(self, team_id: str) -> list[dict]:
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT * FROM sprint_windows WHERE team_id = ? ORDER BY sprint_number",
            (team_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Collaboration snapshots ---

    async def write_collaboration_snapshot(self, team_id: str, sprint_id: int,
                                           metrics: dict) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT OR REPLACE INTO collaboration_snapshots
               (team_id, sprint_id, computed_at,
                gini, entropy_norm, interaction_density, bus_factor,
                clustering_ratio, cadence_regularity, churn_balance,
                attendance_corr, health_score,
                per_member, interaction_graph, lead_time_detail, cadence_detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                team_id, sprint_id, now,
                metrics.get("gini"),
                metrics.get("entropy_norm"),
                metrics.get("interaction_density"),
                metrics.get("bus_factor"),
                metrics.get("clustering_ratio"),
                metrics.get("cadence_regularity"),
                metrics.get("churn_balance"),
                metrics.get("attendance_corr"),
                metrics.get("health_score"),
                json.dumps(metrics.get("per_member", {})),
                json.dumps(metrics.get("interaction_graph", {})),
                json.dumps(metrics.get("lead_time_detail", {})),
                json.dumps(metrics.get("cadence_detail", {})),
            ),
        )
        await self._db.commit()

    async def read_collaboration_snapshots(self, team_id: str) -> list[dict]:
        if not self._db:
            return []
        cursor = await self._db.execute(
            "SELECT * FROM collaboration_snapshots WHERE team_id = ? ORDER BY sprint_id",
            (team_id,),
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        results = []
        for row in rows:
            d = dict(zip(cols, row))
            for json_col in ("per_member", "interaction_graph", "lead_time_detail", "cadence_detail"):
                if d.get(json_col) and isinstance(d[json_col], str):
                    try:
                        d[json_col] = json.loads(d[json_col])
                    except (json.JSONDecodeError, TypeError):
                        pass
            results.append(d)
        return results

    async def read_latest_collaboration(self, team_id: str) -> dict | None:
        if not self._db:
            return None
        cursor = await self._db.execute(
            "SELECT * FROM collaboration_snapshots WHERE team_id = ? ORDER BY sprint_id DESC LIMIT 1",
            (team_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        d = dict(zip(cols, row))
        for json_col in ("per_member", "interaction_graph", "lead_time_detail", "cadence_detail"):
            if d.get(json_col) and isinstance(d[json_col], str):
                try:
                    d[json_col] = json.loads(d[json_col])
                except (json.JSONDecodeError, TypeError):
                    pass
        return d

    # --- Git cache ---

    async def write_git_cache(self, team_id: str, cache_key: str, data: dict) -> None:
        if not self._db:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT OR REPLACE INTO git_cache (team_id, cache_key, computed_at, data) VALUES (?, ?, ?, ?)",
            (team_id, cache_key, now, json.dumps(data)),
        )
        await self._db.commit()

    async def read_git_cache(self, team_id: str, cache_key: str) -> dict | None:
        if not self._db:
            return None
        cursor = await self._db.execute(
            "SELECT data FROM git_cache WHERE team_id = ? AND cache_key = ?",
            (team_id, cache_key),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    # --- Lifecycle ---

    async def close(self) -> None:
        """Graceful shutdown: signal writer, wait for drain, close DB."""
        if self._channel:
            try:
                self._channel.put_nowait(("shutdown", None))
            except asyncio.QueueFull:
                pass
        if self._writer_task:
            try:
                await asyncio.wait_for(self._writer_task, timeout=10.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._writer_task.cancel()
        # Final safety flush
        if self._batch and self._db:
            try:
                await self._db.executemany(_EVENT_INSERT_SQL, self._batch)
                await self._db.commit()
                self._batch = []
            except Exception:
                pass
        if self._db:
            await self._db.close()
