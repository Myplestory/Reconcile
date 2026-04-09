# Reconcile — System Architecture

## What This Is

A real-time integrity monitor for university software engineering courses. Ingests events from project management tools, git repositories, Discord servers, and email archives. Detects anomalies in team attribution and attendance records as they happen. Surfaces findings through a live web dashboard.

Single process. Single event loop. Fully async. Zero polling. Handles 200+ teams on one machine.

---

## System Diagram

```
                         PUSH-BASED INGESTORS
                         (scale with sources, not teams)
    ┌──────────────────────────────────────────────────────────┐
    │                                                          │
    │  Board WS ──────┐  1 persistent WebSocket                │
    │  (wss://...)     │  demux by board_id → team_id          │
    │                  │                                       │
    │  Discord GW ─────┤  1 persistent WebSocket (Gateway)     │
    │  (wss://gw..)    │  demux by guild_id → team_id          │
    │                  │                                       │
    │  GitHub WHK ─────┤  0 outbound (inbound HTTP POST)       │
    │  (POST /hooks)   │  repo name → team_id                  │
    │                  │                                       │
    │  Email IDLE ─────┤  1 persistent IMAP connection          │
    │  (IMAP IDLE)     │  recipient/metadata → team_id          │
    │                  │                                       │
    └────────┬─────────┘                                       │
             │ all events tagged with team_id + priority       │
             ▼                                                 │
    ┌────────────────────────────────────────────────────────┐ │
    │                    EVENT BUS                            │ │
    │                                                        │ │
    │  ┌─────────────────────┐  ┌─────────────────────┐      │ │
    │  │ HIGH PRIORITY (5K)  │  │ LOW PRIORITY (50K)  │      │ │
    │  │                     │  │                     │      │ │
    │  │ card.move           │  │ card.access         │      │ │
    │  │ card.delete         │  │ loaded.board        │      │ │
    │  │ branch.delete       │  │ session.join        │      │ │
    │  │ card.assign         │  │ session.users       │      │ │
    │  │ commit.push         │  │                     │      │ │
    │  │ file.create         │  │                     │      │ │
    │  │ report.submit       │  │                     │      │ │
    │  └──────────┬──────────┘  └──────────┬──────────┘      │ │
    │             │                        │                  │ │
    │             ▼                        ▼                  │ │
    │  ┌──────────────────────────────────────────────┐      │ │
    │  │           BATCH PROCESSOR                    │      │ │
    │  │                                              │      │ │
    │  │  1. Drain all high-priority (up to 500)      │      │ │
    │  │  2. Fill remaining budget from low-priority   │      │ │
    │  │  3. asyncio.gather → all detectors            │      │ │
    │  │  4. Alerts → SSE subscribers + SQLite         │      │ │
    │  │  5. Events → SQLite batch writer              │      │ │
    │  │  6. If alert fired → debounce sweep (30s)     │      │ │
    │  └──────────────────────────────────────────────┘      │ │
    └───────────────────────┬────────────────────────────────┘ │
                            │                                   │
               ┌────────────┼────────────┐                      │
               ▼            ▼            ▼                      │
         DETECTORS     ANALYZER      SQLITE STORE               │
         (per-event)   (on-demand)   (batch writes)             │
                            │            │                      │
                            │            ▼                      │
                            │     ┌──────────────┐              │
                            │     │ reconcile.db │              │
                            │     │ (WAL mode)   │              │
                            │     │ partitioned  │              │
                            │     │ by team_id   │              │
                            │     └──────┬───────┘              │
                            │            │                      │
                            └────────────┘                      │
                                   │                            │
                                   ▼                            │
                         ┌────────────────────┐                 │
                         │   QUART APP        │                 │
                         │                    │                 │
                         │  Dashboard (HTML)  │                 │
                         │  REST API          │                 │
                         │  SSE Streams       │                 │
                         │  Webhook Receiver  │                 │
                         └────────────────────┘                 │
                                   │                            │
                                   ▼                            │
                         Browser (Alpine.js)                    │
                         EventSource (SSE)                      │
                         Zero polling                           │
└───────────────────────────────────────────────────────────────┘
```

---

## Core Principles

1. **Everything is an event.** Git commits, card moves, Discord messages, email arrivals — all normalized to one schema, routed through one bus.
2. **team_id is the partition key.** One bus, one set of detectors, N teams. Detectors partition state internally by `event.team_id`. No per-team bus instances.
3. **Push, not poll.** All four ingestors receive pushed data. Zero polling loops. Zero subprocess calls on the hot path.
4. **Two-tier flow control.** Bus queues use backpressure — when full, ingestors yield (events delayed, not lost). The write channel uses bounded drop — if the 50K channel fills, events are dropped with a warning and recoverable via content-addressable replay (`INSERT OR IGNORE`).
5. **Detectors are plugins.** Drop a `.py` file in `detectors/`. Implement `detect(event) → list[Alert]`. Auto-discovered on startup.
6. **The browser never polls.** SSE pushes alerts and metrics. Alpine re-renders only affected DOM nodes.

---

## Event Schema

```python
@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime          # UTC, from source's time authority
    source: str                  # "board-ws" | "github" | "discord" | "email"
    team_id: str                 # partition key — all routing based on this
    actor: str                   # member ID or "system"
    action: str                  # normalized: "card.move", "commit.push", etc.
    target: str                  # card number, branch name, file path
    target_type: str             # "card" | "branch" | "file" | "report" | "session"
    metadata: dict               # source-specific fields
    raw: dict                    # original payload (audit trail)
    confidence: str              # "server-authoritative" | "client-reported"
    priority: str                # "high" | "low" → queue routing
```

Frozen. Immutable after creation. Slots for low memory. The `team_id` field multiplexes N teams through one bus.

---

## Ingestors

Scale with **data sources**, not with teams. Adding 50 more teams adds zero ingestors.

| Source | Mechanism | Connections | Blocking I/O | Demux Key |
|--------|-----------|:-----------:|:------------:|-----------|
| Board Tool | WebSocket | 1 persistent outbound | None | `board_id` → `team_id` |
| Discord | Gateway WebSocket | 1 persistent outbound | None | `guild_id` → `team_id` |
| GitHub | Webhook (HTTP POST) | 0 outbound | None | `repo.name` → `team_id` |
| Email | IMAP IDLE | 1 persistent outbound | None | recipient/metadata → `team_id` |

### Board Tool WebSocket

Connects to any board tool's WebSocket endpoint. Action map, field names, and source name are all injected — no hardcoded URLs or tool names. Default action map covers common board events:

| WS Action | Normalized Event | Priority |
|-----------|-----------------|----------|
| `moveCard` | `card.move` | high |
| `addcard` | `card.create` | high |
| `delcard` | `card.delete` | high |
| `addMember` | `card.assign` | high |
| `delMember` | `card.unassign` | high |
| `addTag` | `card.tag` | high |
| `delTag` | `card.untag` | high |
| `addDep` | `card.link` | high |
| `updatecard` | `card.update` | low |
| `join` | `session.join` | low |
| `updateusers` | `session.users` | low |

Auto-reconnects on disconnect (503, network error). Exponential backoff.

### Discord Gateway

Connects via `discord.py` or raw Gateway protocol. One bot, all team servers.

| Gateway Event | Normalized Event | Priority |
|--------------|-----------------|----------|
| `MESSAGE_CREATE` | `message.send` | high |
| `MESSAGE_DELETE` | `message.delete` | high |
| `MESSAGE_UPDATE` | `message.edit` | low |
| `PRESENCE_UPDATE` | `session.presence` | low |

Messages are classified on ingestion using the Tier 1 keyword codebook (proactive, outreach, technical help, etc.). Classification tags go into `metadata`.

### GitHub Webhook

Quart route `POST /hooks/github`. Signature verification via `X-Hub-Signature-256`. Configured once at the GitHub org level — covers all team repos.

| GitHub Event | Normalized Event | Priority |
|-------------|-----------------|----------|
| `push` | `commit.push` | high |
| `create` (branch) | `branch.create` | high |
| `delete` (branch) | `branch.delete` | high |
| `pull_request` (opened) | `pr.open` | high |
| `pull_request` (merged) | `pr.merge` | high |
| `pull_request` (closed) | `pr.close` | low |
| `pull_request_review` | `pr.review` | low |

### Email (IMAP IDLE)

Persistent IMAP connection. IDLE command keeps the connection open; the mail server pushes new message notifications. On notification, fetch and parse the `.eml`, extract status report markings, normalize to `report.submit` events.

Fallback: directory watcher (inotify/kqueue) on a local maildir if IMAP is unavailable.

---

## Event Bus

Single bus. Two priority queues. Batch drain. Backpressure.

```python
class EventBus:
    high_queue: asyncio.Queue[Event]    # maxsize=5,000
    low_queue: asyncio.Queue[Event]     # maxsize=50,000
    detectors: list[BaseDetector]       # one instance each, state partitioned by team_id
    subscribers: list[asyncio.Queue]    # SSE consumers
```

### Processing Loop

```
while running:
    batch = []
    
    # Phase 1: drain all high-priority (up to 500)
    while high_queue not empty and len(batch) < 500:
        batch.append(high_queue.get_nowait())
    
    # Phase 2: fill remaining budget from low-priority
    while low_queue not empty and len(batch) < 500:
        batch.append(low_queue.get_nowait())
    
    # Phase 3: if nothing, wait 100ms for next high event
    if not batch:
        try:
            event = await wait_for(high_queue.get(), timeout=0.1)
            batch.append(event)
        except TimeoutError:
            continue
    
    # Phase 4: enqueue events to write channel (non-blocking)
    for event in batch:
        store.enqueue_event(event)     # → write channel → batched by writer
    
    # Phase 5: run all detectors on batch
    for event in batch:
        results = await asyncio.gather(*(d.detect(event) for d in detectors))
        for alerts in results:
            for alert in alerts:
                counters.record(alert)         # CQRS: O(1) in-memory
                store.enqueue_alert(alert)     # durability fence → flushes pending events
                → push to SSE subscribers
                → trigger debounced sweep for alert.team_id
```

### Backpressure

When a bus queue hits `maxsize`, `await queue.put()` suspends the ingestor coroutine. The event loop continues processing other tasks. When the bus drains below the limit, the ingestor resumes. Events are delayed at this stage.

After bus processing, events are enqueued to the write channel via non-blocking `put_nowait()`. If the write channel is full (50K capacity), events are dropped with a warning log. Dropped events are recoverable via replay — `INSERT OR IGNORE` on `event_hash` ensures idempotent re-ingestion.

### Sprint Night Throughput

| Metric | Value |
|--------|-------|
| Peak event rate (50 teams × 250 users) | ~50 events/sec burst |
| Queue capacity | 55,000 events (5K high + 50K low) |
| Buffer at peak | 18 minutes before backpressure |
| Batch drain throughput | ~5,000 events/sec |
| Headroom | 100× over peak burst |

---

## Detectors

Each detector is one Python file. Auto-discovered from `detectors/` directory. All detectors receive all events. State partitioned by `team_id`.

```python
class BaseDetector:
    name: str
    
    def __init__(self):
        self.state: dict[str, dict] = {}    # team_id → detector state
    
    async def detect(self, event: Event) -> list[Alert]:
        tid = event.team_id
        if tid not in self.state:
            self.state[tid] = self._init_team_state()
        team = self.state[tid]
        # ... detection logic
    
    def evict_team(self, team_id: str):
        """Free memory for archived semesters."""
        self.state.pop(team_id, None)
```

### Built-In Detectors

| Detector | Category | Trigger | Watches For |
|----------|----------|---------|-------------|
| `zero-commit-complete` | attribution | `card.move → Complete` | Linked branch has 0 commits |
| `branch-delete-before-complete` | evidence | `branch.delete` then `card.move → Complete` | Deletion within N seconds of completion |
| `batch-completion` | process | `card.move → Complete` | N+ cards completed by same actor in window |
| `file-reattribution` | attribution | `commit.push` with file changes | Byte-identical content under different author |
| `completion-non-assignee` | process | `card.move → Complete` | Mover is not assignee and not PM |
| `unrecorded-deletion` | evidence | `branch.delete` (GitHub) | No corresponding board event |
| `report-revision` | attendance | `report.submit` | Different markings than prior report |
| `attendance-anomaly` | attendance | `session.present/absent` | Presence without activity, absence without notice, streaks |

See [docs/detectors.md](../docs/detectors.md) for configurable parameters and thresholds.

### Custom Detectors

Drop a `.py` file in `reconcile/detectors/`. Implement `BaseDetector`. It's loaded on startup. No registration, no config change.

```python
# reconcile/detectors/my_custom_detector.py

from reconcile.detectors.base import BaseDetector
from reconcile.schema import Event, Alert

class MyDetector(BaseDetector):
    name = "my-custom-check"
    
    async def detect(self, event: Event) -> list[Alert]:
        # your logic here
        return []
```

---

## Historical Analyzer

Runs on-demand, not per-event. Three trigger modes:

| Mode | Trigger | Behavior |
|------|---------|----------|
| On-anomaly | Detector fires alert | 30-second debounce per team. Timer resets on each alert. Sweep runs once after burst settles. |
| On-schedule | Configurable interval | Default: daily 03:00 UTC. Per-team override available. |
| On-demand | API call or CLI | `POST /api/teams/{id}/sweep` or `--sweep` flag |

The analyzer reads the team's event timeline from SQLite, computes member profiles (perpetrator/victim scores, flag accumulation, direction classification), and writes results to the `profiles` table.

Runs in `run_in_executor` for CPU-bound profile computation. Does not block the event loop.

### Debounce Logic

```
Sprint night: alerts fire every few seconds for team-a
    
    Alert at t=0   → start 30s timer for team-a
    Alert at t=5   → reset timer (now 30s from t=5)
    Alert at t=12  → reset timer (now 30s from t=12)
    Alert at t=18  → reset timer (now 30s from t=18)
    ... burst ends ...
    t=48 (30s after last alert) → sweep runs ONCE for team-a
```

---

## Storage — SQLite + Write Channel

Single file: `data/reconcile.db`. WAL mode. Partitioned by `team_id` column.

### Schema

```sql
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    event_hash TEXT NOT NULL UNIQUE,     -- content-addressable dedup (SHA-256, 16 chars)
    team_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    ingested_at TEXT NOT NULL,           -- when Reconcile received it
    source TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    target_type TEXT NOT NULL DEFAULT '',
    metadata JSON,
    confidence TEXT NOT NULL DEFAULT 'server-authoritative',
    priority TEXT NOT NULL DEFAULT 'high'
);
CREATE INDEX idx_events_team ON events(team_id, timestamp);
CREATE INDEX idx_events_hash ON events(event_hash);

CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    detector TEXT NOT NULL,
    severity TEXT NOT NULL CHECK(severity IN ('info', 'elevated', 'suspect', 'critical')),
    category TEXT NOT NULL DEFAULT 'process' CHECK(category IN ('process', 'attendance', 'attribution', 'evidence')),
    score INTEGER NOT NULL DEFAULT 2,    -- composite: category_weight × severity_weight (1-16)
    title TEXT NOT NULL,
    detail TEXT,
    metadata JSON,
    event_hash TEXT                       -- links alert to triggering event
);
CREATE INDEX idx_alerts_team ON alerts(team_id, timestamp);
CREATE INDEX idx_alerts_severity ON alerts(team_id, severity, timestamp);
CREATE INDEX idx_alerts_event ON alerts(event_hash);

CREATE TABLE profiles (
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
    version INTEGER NOT NULL DEFAULT 1,  -- append-only: each sweep creates a new version
    profile_hash TEXT DEFAULT ''          -- content-addressable dedup (SHA-256, 16 chars)
);
CREATE INDEX idx_profiles_team ON profiles(team_id, member, version);

CREATE TABLE teams (
    team_id TEXT PRIMARY KEY,
    team_name TEXT,
    semester TEXT,
    config JSON NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'stopped', 'archived')),
    created_at TEXT NOT NULL
);

CREATE TABLE branch_resolutions (
    team_id TEXT NOT NULL,
    branch TEXT NOT NULL,
    resolved_author TEXT,
    resolution_method TEXT NOT NULL,  -- corroborated/majority/conflict/single-source/unresolvable-*
    evidence_quality TEXT NOT NULL,   -- git-verifiable/board-verifiable/heuristic/disputed/unresolvable
    signals JSON NOT NULL,           -- {"git_ref": "...", "board_linker": "...", "commit_author": "..."}
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (team_id, branch)
);

CREATE TABLE metrics (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    events_total INTEGER,
    events_per_min REAL,
    alerts_total INTEGER,
    ingestor_status JSON,
    detector_status JSON
);
CREATE INDEX idx_metrics_team ON metrics(team_id, timestamp);
```

### Write Channel (CQRS, Single-Writer)

All writes go through a single-writer channel. No locks, no races, no concurrent flushes.

```
bus._process_events()
    ├── store.enqueue_event(event)  →  channel.put(("event", data))
    └── store.enqueue_alert(alert)  →  channel.put(("alert_fence", data))
                                            │
                            ┌────────────────┘
                            ▼
                   Single Writer Coroutine
                   ├── "event"       → append to batch (up to 500)
                   ├── "alert_fence" → flush batch + commit alert atomically
                   ├── "profiles"    → atomic multi-row insert
                   ├── periodic (5s) → flush batch if non-empty
                   └── "shutdown"    → drain batch, exit
                            │
                            ▼
                        SQLite WAL (synchronous=FULL)
```

**Bounded queue** (50K items). If full, events are dropped with a warning log — the content-addressable `event_hash` allows replay recovery. Alerts log at ERROR level if dropped.

**Read path** goes direct to DB, bypassing the channel. WAL mode ensures concurrent reads see consistent snapshots during writes.

**Why not separate read/write pools?** SQLite's locking is file-level. Multiple connections just contend. With WAL, one connection handles concurrent reads + writes efficiently. Separate pools are for Postgres/MySQL scale (thousands of QPS), not our workload.

### Causal Ordering via Alert Fences

Alerts act as **durability fences**. When a detector fires an alert:

1. All pending events in the writer batch are flushed
2. The alert is written
3. A single `COMMIT` covers both

This guarantees: if an alert references an `event_hash`, that event is always committed before (or with) the alert. No causal gaps.

```
Events:  [e1, e2, e3]  ──── ALERT (fence) ────  [e4, e5]
                              │
                              ▼
                     Single COMMIT: e1, e2, e3 + alert
```

Between alert fences, the periodic flush (5s) bounds the data loss window for quiet periods.

### CQRS Alert Counters

In-memory write-side projection. Updated O(1) on every `_emit_alert()`. Read by the SSE metrics stream with zero DB cost.

```python
class AlertCounters:
    by_severity: dict[str, int]   # {"critical": 2, "suspect": 1, ...}
    by_category: dict[str, int]   # {"evidence": 3, "attendance": 1, ...}
    total: int
```

The dashboard's metrics SSE reads `bus.alert_counters.snapshot()` — no DB queries on the 5-second hot path.

### Sweep Deduplication (Content Hash)

Each sweep produces a SHA-256 content hash of the profile snapshot (direction + scores + flag count per member, deterministic JSON serialization). Before writing:

1. Compare `new_hash` against `_last_profile_hash[team_id]` (in-memory)
2. If unchanged → skip DB write + skip permutation test
3. If changed → write profiles with hash, update `_last_profile_hash`

Hash stored in `profiles` table alongside version. On startup, hydrate from latest version. Also serves as tamper detection — recompute hash from stored profile data, compare against stored hash.

```python
def _profile_hash(profiles: dict) -> str:
    canonical = json.dumps(
        {m: {"d": p.direction, "p": p.perpetrator_score, "v": p.victim_score,
             "f": len(p.flags), "c": p.commits}
         for m, p in sorted(profiles.items())},
        sort_keys=True
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
```

### Crash Durability

| Scenario | Data at Risk | Recovery |
|----------|-------------|----------|
| SIGTERM / Ctrl+C | None | Graceful shutdown drains writer, commits all pending |
| SIGKILL / power loss | Events since last fence or periodic flush (≤5s) | `synchronous=FULL` + WAL guarantees committed data survives |
| Crash mid-batch | Uncommitted batch | WAL rollback restores last consistent state |
| Crash mid-alert-fence | Atomic: all-or-nothing | Either events+alert commit together, or neither does |

**`PRAGMA synchronous=FULL`**: every commit is synced to disk. ~1-2ms latency per commit, but guarantees committed data survives OS crashes and power loss.

**Graceful shutdown sequence:**
1. Signal handler sets `shutdown_event`
2. Bus stops processing, ingestors cancelled
3. Store receives `("shutdown", None)` on channel
4. Writer drains all pending events/alerts, commits
5. DB connection closed
6. Process exits

### Event Sourcing

Events are content-addressable via `event_hash` (SHA-256 of actor + action + target + timestamp, truncated to 16 chars). `INSERT OR IGNORE` deduplicates on replay.

Profiles are append-only with monotonic `version` numbers. Each sweep creates a new version — previous versions are preserved for audit trail. Historical queries: `read_profiles(team_id, version=3)`.

### Semester Archiving

```sql
UPDATE teams SET status = 'archived' WHERE semester = 'Fall-2025';
```

Archived teams: data stays in SQLite (queryable for historical review). Detector state evicted from memory. Not included in live monitoring. Dashboard filters by active semester by default.

### Capacity

| Scale | Events/Semester | DB Size | Live Memory |
|-------|:---:|:---:|:---:|
| 50 teams | 500K | ~50MB | ~250MB |
| 200 teams | 2M | ~200MB | ~1GB |
| 500 teams | 5M | ~500MB | Archive older semesters |

---

## Author Resolution — Triangulation with Durable Cache

Git branch refs are mutable — deletable, force-pushable, GC-able. When a branch ref is lost, the author becomes unknown. The pipeline triangulates authorship from 3 independent sources rather than trusting any single one.

### Trust Hierarchy

| Source | Mutability | What It Proves |
|--------|-----------|----------------|
| Git commit DAG | Immutable (SHA-1) | Topology, commit authorship |
| Discord Snowflakes | Immutable (server-generated) | Timestamp, message author |
| SMTP headers | Immutable (relay chain) | Delivery timestamps |
| Board activity log | Append-only, client-auth | Actor, action, timestamp |
| **Git branch refs** | **Mutable** | **Nothing once gone** |

### Triangulation

```
Signal 1: git_ref          (first_unique_author from branch ref)
Signal 2: board_linker     (first addgithub event for branch)
Signal 3: commit_author    (oldest unique commit on branch)
                ↓
         ┌──────────────────────────────────────┐
         │  3 agree → corroborated (verifiable) │
         │  2 agree → majority (disputed)       │
         │  1 only  → single-source (heuristic) │
         │  0       → unresolvable              │
         │  all disagree → conflict (disputed)  │
         └──────────────────────────────────────┘
```

Each resolution is tagged with `resolution_method` and `evidence_quality`. Downstream scoring weights accordingly.

### Durable Cache

Resolutions persist to `branch_resolutions` table via the write channel. On each sweep, the analyzer compares new resolutions against stored ones. If resolution quality degraded (e.g., `corroborated` → `unresolvable`), an `evidence_degradation` alert fires.

This turns invisible data loss (GC'd refs) into a detectable, timestamped event.

### Self-Absorption Filtering

When the resolved author matches the child commit author, it's normal merge workflow — not cross-author absorption. These are filtered from findings. Only genuine cross-author cases are scored.

---

## Dashboard — Quart + Alpine.js + Tailwind + 3d-force-graph

### Stack

```html
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3/dist/cdn.min.js"></script>
<!-- 3d-force-graph loaded lazily on first team select (~600KB) -->
```

No build step. No node_modules. One HTML file. Two eager CDN tags + one lazy-loaded.

### API Routes

| Method | Path | Function |
|--------|------|----------|
| GET | `/` | Dashboard HTML |
| GET | `/api/teams` | List teams (status, event/alert counts) |
| POST | `/api/teams` | Add team |
| GET | `/api/teams/:id` | Team detail (profiles) |
| DELETE | `/api/teams/:id` | Remove team |
| GET | `/api/teams/:id/config` | Read detector thresholds + sweep settings |
| PATCH | `/api/teams/:id/config` | Update config (live, no restart) |
| POST | `/api/teams/:id/sweep` | Trigger historical sweep |
| GET | `/api/teams/:id/alerts` | Alert log (paginated, filterable) |
| GET | `/api/teams/:id/events` | Event log (paginated) |
| POST | `/api/teams/:id/inject` | Inject synthetic event (test/replay) |
| GET | `/api/alerts/:id` | Single alert detail (triggering event + related) |
| GET | `/api/alerts/stream` | SSE — live alerts (all teams) |
| GET | `/api/metrics/stream` | SSE — system metrics (5s, CQRS counters) |
| GET | `/api/logs/stream` | SSE — structured system logs (ingest, detect, sweep) |
| POST | `/hooks/github` | GitHub webhook receiver (HMAC verified) |
| GET | `/api/health` | Health check |

See [docs/api.md](../docs/api.md) for full request/response examples.

### Dashboard Layout

```
┌─────────┬─────────────────────────────────────────┐
│ TEAMS   │  ┌─ TABS ─────────────────────────────┐ │
│ (list)  │  │ [Alerts] [Members] [Timeline] [3D] │ │
│         │  ├─────────────────────────────────────┤ │
│ click → │  │                                     │ │
│ loads   │  │  MAIN CONTENT AREA                  │ │
│ detail  │  │  (switches by tab)                  │ │
│         │  ├─────────────────────────────────────┤ │
│         │  │  SYSTEM LOG (always visible)        │ │
│         │  │  Queue: H:3570 L:14438 | 8 det     │ │
│         │  │  12:03:01 [ingest] board ws msg     │ │
│         │  └─────────────────────────────────────┘ │
└─────────┴─────────────────────────────────────────┘
```

**1. Team Grid** (left sidebar)
Cards per team. Name, status indicator (green/yellow/red), alert count badge (color-coded by max severity). Searchable. Sorted by alert count. Click → loads detail into main area.

**2. Tabbed Content Area** (main)
- **Alerts tab** (default): SSE-driven live feed. Color-coded by severity. Filterable by team, detector, severity, category. Auto-scrolling with pause-on-hover. Click any alert → evidence detail modal.
- **Members tab**: Member profiles grid (direction, scores, flags, activity counters). Full-width layout. Expandable detail per member.
- **Timeline tab**: Chronological event stream for selected team. Mixed sources with source-colored pills.
- **Graph tab**: 3D force-directed relationship graph (lazy-loaded). Full-width rendering. Interactive rotation/zoom.

**3. Alert Detail Modal** (on alert click)
Evidence chain: triggering event (via `event_hash` join), related alerts (same team + detector, temporal proximity). Audit trail: detector name, timestamp, event hash. Severity/category/score breakdown.

**4. Bottom Log Bar** (always visible, collapsible)
SSE-driven structured log feed from `/api/logs/stream`. Color-coded by source: green=ingest, yellow=detect, blue=sweep, red=error. LRU eviction (200 entries). Auto-scroll with pause-on-scroll-up. Shows sweep completions with profile hash, detector fires with alert title, ingestor connects/errors.

**5. Runtime Config Panel** (drawer)
Per-team detector toggles, threshold sliders, sweep settings (on_alert, debounce, interval). Live PATCH — no restart.

### Data Flow to Browser

```
Detector fires alert
    → bus pushes to asyncio.Queue per SSE subscriber
    → Quart SSE route: await queue.get()
    → yields "data: {json}\n\n"
    → browser EventSource receives
    → Alpine: x-data.alerts.unshift(alert)
    → DOM: one <div> prepended (surgical re-render)
```

No browser polling. Three persistent HTTP connections (alerts, metrics, logs). Server pushes when data exists. Alpine reactivity updates only the affected DOM nodes.

---

## Configuration

Copy `config_template.py` → `config_local.py`. Edit values.

```python
# Teams (add/remove/modify without restart via API)
TEAMS = [
    TeamConfig(team_id="team-a", team_name="Team Alpha", ...),
    TeamConfig(team_id="team-b", team_name="Team Beta", ...),
    # ... 200+ teams
]

# Ingestor settings
WS_URL = "wss://your-board-tool/ws"
GITHUB_WEBHOOK_SECRET = "..."
DISCORD_BOT_TOKEN = "..."
IMAP_HOST = "..."

# Detector thresholds
DETECTORS = {
    "batch-completion": {"window_seconds": 60, "min_cards": 3},
    "branch-delete-complete": {"window_seconds": 300},
    ...
}

# Sweep behavior
SWEEP_ON_ALERT = True
SWEEP_DEBOUNCE_SECONDS = 30
SWEEP_SCHEDULE_INTERVAL = 86400  # daily

# Dashboard
DASHBOARD_PORT = 8080
```

---

## File Structure

```
reconcile/
├── __init__.py
├── __main__.py                # Batch pipeline CLI: python -m reconcile
├── schema.py                  # Event, Alert dataclasses, Category, composite_score
├── bus.py                     # EventBus: priority queues, AlertCounters (CQRS)
├── analyzer.py                # HistoricalAnalyzer: sweep, profiles, scoring
├── storage.py                 # Store: write channel, alert fences, WAL, synchronous=FULL
├── orchestrator.py            # Orchestrator: wires ingestors → bus → detectors → store
├── pipeline.py                # Batch pipeline runner (ingest → analyze → forensics → output)
├── main.py                    # Live CLI: --serve / --live / --batch / --sweep
├── config.py                  # Config loading (YAML/dict)
├── config_template.py         # All knobs in one file
│
├── ingestors/                 # Real-time push ingestors
│   ├── ws_board.py            # Board tool WebSocket (1 conn, demux board_id)
│   └── git_poll.py            # Git polling ingestor
│
├── ingest/                    # Batch data loading (file-based)
│   ├── git.py                 # Load commits, branches, files from repo
│   ├── board.py               # Load board events/cards from JSON
│   ├── discord.py             # Load Discord messages (snowflake timestamps)
│   ├── email.py               # Load status reports from maildir
│   ├── snapshot.py            # Git snapshot capture
│   └── source.py              # Source base classes
│
├── normalize/                 # Data type normalization
│   ├── types.py               # PipelineState, Commit, Card, Message, Report
│   └── timeline.py            # Unified timeline building
│
├── analyze/                   # Batch cross-referencing & scoring
│   ├── dag.py                 # Commit DAG building (forward/inverted indices)
│   ├── provenance.py          # Branch provenance + ancestry resolution
│   ├── invariants.py          # 7 parameterized invariant checks
│   ├── scoring.py             # Member scoring (perpetrator/victim)
│   ├── pairs.py               # Pair analysis (Harary chain, hub dedup, SNA)
│   └── discord.py             # Message classification framework
│
├── forensics/                 # Forensic verification
│   ├── snowflake.py           # Discord snowflake timestamp validation
│   ├── manifest.py            # Evidence manifest generation
│   ├── smtp.py                # Email SMTP concordance
│   └── consent.py             # Digital consent search
│
├── detectors/                 # Real-time anomaly detectors (auto-discovered)
│   ├── __init__.py            # discover_detectors(): pkgutil scan
│   ├── base.py                # BaseDetector: team_state(), get_config(), category
│   ├── zero_commit_complete.py       # category=attribution
│   ├── branch_delete_complete.py     # category=evidence
│   ├── batch_completion.py           # category=process
│   ├── file_reattribution.py         # category=attribution
│   ├── completion_non_assignee.py    # category=process
│   ├── unrecorded_deletion.py        # category=evidence
│   ├── report_revision.py            # category=attendance
│   └── attendance_anomaly.py         # category=attendance
│
├── outputs/                   # Real-time alert outputs
│   ├── console.py             # Terminal alerts (color-coded)
│   └── json_file.py           # Append-only JSONL backup
│
├── output/                    # Batch output generation
│   ├── json_artifacts.py      # Serialize PipelineState to JSON
│   ├── markdown.py            # Generic report template
│   ├── presentation.py        # Presentation layer
│   └── visuals.py             # Visualizations
│
├── provisioning/              # Team/Discord setup
│   ├── discord.py             # Discord server auto-creation + lifecycle
│   └── team_import.py         # Bulk team import (JSON/CSV)
│
├── web/                       # Dashboard & API
│   ├── app.py                 # Quart: REST + webhook + config PATCH
│   ├── sse.py                 # SSE: alert stream + metrics (CQRS counters) + logs
│   └── templates/
│       └── dashboard.html     # Alpine.js + Tailwind + 3d-force-graph (lazy)
│
└── tests/                     # 132+ tests
    ├── conftest.py            # Fixtures: event_factory, bus, store
    ├── test_schema.py         # Event immutability, priorities, categories, scoring
    ├── test_bus.py            # Priority queues, batch drain, AlertCounters
    ├── test_storage.py        # WAL, CRUD, dedup, profile versioning, channel
    ├── test_durability.py     # Crash simulation, load stress, fence integrity, WAL recovery
    ├── test_detectors.py      # All 8 detectors, auto-discovery, team isolation
    ├── test_analyzer.py       # Sweep scoring, direction, structured flags
    ├── test_ingestors.py      # BoardWS, GitPoll
    ├── test_orchestrator.py   # Multi-team, reverse mappings
    ├── test_provisioning.py   # JSON/CSV parsing
    └── test_web.py            # API routes, config endpoints, generic safety
```

### Dependencies

```
pip install quart aiosqlite websockets discord.py
```

Four packages. Everything else is stdlib.

---

## Deployment

```bash
# Quick start
pip install quart aiosqlite websockets discord.py
cp reconcile/config_template.py reconcile/config_local.py
# Edit config_local.py
python -m reconcile.main --serve --port 8080
```

### Production (university server)

```
systemd service     → auto-restart on crash
nginx reverse proxy → TLS termination, /hooks/github forwarding
logrotate           → manage JSONL backup files
cron                → semester archival script
```

Single process. No Docker required. No external database. Runs on any machine with Python 3.11+ and network access to configured data sources.

---

## Batch Pipeline

One-shot analysis of existing data. Complements the real-time system.

```bash
python -m reconcile                          # Full pipeline
python -m reconcile --phase ingest analyze   # Specific phases
python -m reconcile --verify                 # Evidence manifest only
```

### Phases

```
ingest → normalize → analyze → forensics → output
```

| Phase | Modules | Purpose |
|-------|---------|---------|
| **ingest** | `ingest/git.py`, `board.py`, `discord.py`, `email.py` | Load raw data from files/repos |
| **normalize** | `normalize/types.py`, `timeline.py` | Build PipelineState with typed objects (Commit, Card, Message, Report) |
| **analyze** | `analyze/dag.py`, `provenance.py`, `invariants.py`, `scoring.py`, `pairs.py`, `discord.py` | Cross-reference, score, detect invariant violations |
| **forensics** | `forensics/snowflake.py`, `manifest.py`, `smtp.py`, `consent.py` | Validate evidence authenticity |
| **output** | `output/json_artifacts.py`, `markdown.py`, `visuals.py` | Generate reports and artifacts |

### Analysis Pipeline Detail

```
Commit DAG (dag.py)           Invariant Checks (invariants.py)
├── Forward/inverted indices  ├── 7 parameterized checks
├── Fork-point resolution     ├── Zero-commit-complete
├── Orphan reconstruction     ├── Branch-delete-before-merge
└── Branch provenance         ├── Duplicate file content
    (provenance.py)           └── Attendance correlation
                              
Scoring (scoring.py)          Pair Analysis (pairs.py)
├── Pass 1: Member profiles   ├── Harary chain detection
├── Pass 2: PM dimensions     ├── Hub deduplication
├── Permutation test           └── SNA metrics
└── p-value computation
```

---

## Scale Summary

| What | How |
|------|-----|
| 200+ teams | Single bus, `team_id` multiplexing, detector state partitioning |
| Sprint night burst | Priority queues + batch drain + backpressure. 100× headroom over peak. |
| 4 data sources | 2 WebSockets + 1 webhook + 1 IMAP. Zero polling. |
| Storage | SQLite WAL + `synchronous=FULL`. Single-writer channel. 200MB/semester. |
| Crash durability | Alert fences guarantee causal ordering. ≤5s data loss window. |
| Dashboard | SSE push to browser. CQRS counters (zero DB queries on hot path). |
| New detector | Drop a .py file. No config change. Auto-discovered. |
| New team | API call or config edit. No restart. |
| Semester turnover | One SQL UPDATE. Archived data stays queryable. Memory freed. |
