# Storage & Durability

Source: [`reconcile/storage.py`](../reconcile/storage.py)

---

## Architecture

Single SQLite file (`data/reconcile.db`). WAL mode. `PRAGMA synchronous=FULL`. One writer, concurrent readers.
Inspired by production grade crash recovery designed for PolyEdge.trade. Respects CQRS

```
Producers (bus, sweep, inject)
    │
    ├── store.enqueue_event(event)      non-blocking put_nowait
    ├── store.enqueue_alert(alert)      non-blocking put_nowait (FENCE)
    └── store.enqueue_profiles(...)     non-blocking put_nowait
    │
    ▼
asyncio.Queue (bounded 50K)
    │
    ▼
Single Writer Coroutine (_writer_loop)
    ├── "event"       → append to batch (up to 500)
    ├── "alert_fence" → flush batch + commit alert atomically
    ├── "profiles"    → atomic multi-row insert + commit
    ├── timeout (5s)  → flush batch if non-empty
    └── "shutdown"    → drain batch, exit
    │
    ▼
SQLite WAL (synchronous=FULL)
    │
    ▼
Readers (dashboard, API, SSE)
    └── read_events(), read_alerts(), read_profiles() → direct DB queries
```

---

## Write Channel

All writes flow through a bounded `asyncio.Queue(maxsize=50_000)`. A single writer coroutine is the exclusive consumer. No locks, no races, no concurrent flushes.

**Enqueue methods** (all non-blocking, called from bus coroutines):

| Method | Message Type | Behavior |
|--------|-------------|----------|
| [`enqueue_event()`](../reconcile/storage.py) | `"event"` | Serializes Event → dict, queues. Drops on full (warning). |
| [`enqueue_alert()`](../reconcile/storage.py) | `"alert_fence"` | Serializes Alert → tuple, queues. Drops on full (error). |
| [`enqueue_profiles()`](../reconcile/storage.py) | `"profiles"` | Queues (team_id, profiles dict). Drops on full (error). |

**Writer flush triggers:**

| Trigger | Condition | Guarantees |
|---------|-----------|------------|
| Batch size | `len(batch) >= 500` | Events flushed when batch fills |
| Alert fence | Alert enqueued | All pending events + alert in one `COMMIT` |
| Periodic | 5 seconds since last flush | Bounds idle data loss window |
| Shutdown | `("shutdown", None)` received | Drains all remaining events |

---

## Causal Ordering (Alert Fences)

Alerts act as durability fences. When a detector fires:

```
Events:  [e1, e2, e3]  ─── ALERT (fence) ───  [e4, e5]
                              │
                              ▼
                    Single COMMIT: e1, e2, e3 + alert
```

**Guarantee:** If an alert exists in the DB, every event it references (via `event_hash`) is also committed. No causal gaps.

Implementation: [`_flush_with_alert()`](../reconcile/storage.py) calls `executemany()` for pending events, then `execute()` for the alert, then a single `commit()`.

---

## CQRS Alert Counters

In-memory write-side projection in [`AlertCounters`](../reconcile/bus.py). Updated O(1) on every alert emit. Read by the SSE metrics stream with zero DB cost.

```python
class AlertCounters:
    by_severity: dict[str, int]   # {"critical": 2, "suspect": 5, ...}
    by_category: dict[str, int]   # {"evidence": 3, "attendance": 1, ...}
    total: int
```

The `/api/metrics/stream` SSE reads `bus.alert_counters.snapshot()` — no DB queries on the 5-second hot path. Counters are session-scoped (reset on restart). Historical counts come from `alert_count()` on the teams list endpoint.

---

## Crash Recovery

| Scenario | Data at Risk | Recovery |
|----------|-------------|----------|
| `SIGTERM` / `Ctrl+C` | None | Graceful: signal handler → shutdown_event → writer drains → commit → close |
| `SIGKILL` / power loss | Events since last fence or periodic flush (max 5s) | `synchronous=FULL` guarantees committed data survives. WAL auto-recovers. |
| Crash mid-batch | Uncommitted batch | WAL rollback restores last consistent state |
| Crash mid-fence | Atomic: all-or-nothing | Events+alert commit together, or neither does |
| Crash mid-profile-write | Partial version | All member inserts in one implicit transaction; rollback on failure |

**Graceful shutdown sequence** (in [`Store.close()`](../reconcile/storage.py)):

1. Enqueue `("shutdown", None)` to channel
2. Wait up to 10s for writer to drain and exit
3. If timeout: cancel writer task
4. Safety flush: if `_batch` non-empty, `executemany` + `commit` directly
5. Close DB connection

---

## WAL Mode

Write-Ahead Logging enables concurrent reads during writes. Readers see a consistent snapshot at the time their transaction started. Writers append to the WAL file; periodic checkpoints merge WAL back into the main DB.

**PRAGMAs:**

| Setting | Value | Why |
|---------|-------|-----|
| `journal_mode` | `WAL` | Concurrent reads + writes |
| `synchronous` | `FULL` (2) | Committed data survives OS crash / power loss |

**Why not `synchronous=NORMAL`?** In WAL mode, `NORMAL` syncs the main DB but not WAL checkpoints. On OS crash, committed data in the WAL can be lost. `FULL` adds ~1-2ms per commit but guarantees durability.

**Why not separate read/write connections?** SQLite locking is file-level. Multiple connections contend rather than parallelize. With WAL, one connection handles both paths efficiently. Separate pools are for Postgres/MySQL scale.

---

## Event Sourcing

Events are content-addressable via `event_hash`: SHA-256 of `actor + action + target + timestamp`, truncated to 16 hex chars. Computed as a property on the frozen [`Event`](../reconcile/schema.py) dataclass.

`INSERT OR IGNORE` on the `UNIQUE(event_hash)` constraint provides idempotent ingestion. Replaying the same events produces no duplicates.

Profiles are append-only with monotonic `version` numbers. Each sweep appends a new version — previous versions preserved for audit trail:

```sql
-- Latest profiles
SELECT * FROM profiles WHERE team_id = ? AND version = (SELECT MAX(version) ...)

-- Historical query
SELECT * FROM profiles WHERE team_id = ? AND version = 3
```

---

## Schema

See [`docs/schema.md`](schema.md) for full data model documentation.

Tables: `events`, `alerts`, `profiles`, `teams`, `metrics`, `discord_servers`. All partitioned by `team_id`.
