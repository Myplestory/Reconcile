# Reconcile

**Real-time integrity engine for software team projects.**

Ingests events from project management tools, git repositories, Discord servers, and email archives — in real time, via push. Detects anomalies in team attribution and attendance records as they happen. Surfaces findings through a live web dashboard.

When contribution is verified from data, physical presence becomes a policy choice — not a measurement constraint.

Single process. Single event loop. Fully async. Zero polling. Handles 200+ teams on one machine.

---

## How It Works

```
    Ingestors (swappable)                 Single Event Bus                    Dashboard
    ═════════════════════                 ════════════════                    ═════════

    ┌─ Source Control ────┐
    │  GitHub Webhooks    │──┐            ┌──────────────┐
    │  ╌ GitLab, Bitbucket│  │            │ Priority     │     Detectors
    ├─ Project Board ─────┤  │ normalize  │ Queues       │ ──→ (7 built-in,  ──→ Alerts
    │  WebSocket          │──┤ ────────→  │ Batch Drain  │     drop-in custom)
    │  ╌ Jira, Trello     │  │ team_id    └──────┬───────┘
    ├─ Team Comms ────────┤  │                   │
    │  Discord Gateway    │──┤            ┌──────┴───────┐
    │  ╌ Slack, Teams     │  │            │   SQLite     │ ←── Analyzer
    ├─ Accountability ────┤  │            │   (WAL)      │     (on-anomaly,
    │  Email IMAP IDLE    │──┘            └──────┬───────┘      on-schedule,
    │  ╌ LMS API          │                      │              on-demand)
    └─────────────────────┘                      ▼
                                           SSE → Browser
     ╌ = write an ingestor               (Alpine.js + Tailwind + 3d-force-graph)
```

- **Zero polling.** Board and Discord push via WebSocket. GitHub pushes via webhook. Email pushes via IMAP IDLE.
- **One bus, N teams.** Events multiplexed by `team_id`. Detectors partition state internally. Adding 50 teams adds zero ingestors.
- **Sprint night ready.** Priority queues + batch drain + backpressure. 100x headroom over peak burst.

---

## Quick Start

```bash
pip install quart aiosqlite websockets discord.py

cp reconcile/config_template.py reconcile/config_local.py
# Edit config_local.py: teams, tokens, webhook secret

python -m reconcile.main --serve --port 8080
# → Dashboard at http://localhost:8080
```

### CLI Modes

```bash
python -m reconcile.main --serve          # Dashboard + live monitoring
python -m reconcile.main --live           # Live monitoring, no dashboard
python -m reconcile.main --batch          # One-shot analysis of existing data
python -m reconcile.main --sweep          # Historical sweep only, then exit
```

---

## Architecture

Full architecture document: [ARCHITECTURE.md](ARCHITECTURE.md)

### Ingestors (push-based, shared)

| Source | Mechanism | Connections | Demux Key |
|--------|-----------|:-----------:|-----------|
| Project Board | WebSocket | 1 persistent | `boardid` → `team_id` |
| Discord | Gateway WebSocket | 1 persistent | `guild_id` → `team_id` |
| GitHub | Webhook (POST) | 0 outbound | `repo.name` → `team_id` |
| Email | IMAP IDLE | 1 persistent | metadata → `team_id` |

### Detectors (pluggable)

| Detector | Category | Watches For |
|----------|----------|-------------|
| `zero_commit_complete` | attribution | Card completed with 0 commits on linked branch |
| `branch_delete_complete` | evidence | Branch deleted within N seconds before card completed |
| `batch_completion` | process | N+ cards completed by same actor in rapid succession |
| `file_reattribution` | attribution | File deleted and re-added byte-identical under different author |
| `completion_non_assignee` | process | Card completed by someone other than assignee or PM |
| `unrecorded_deletion` | evidence | Branch deleted in git with no board record |
| `report_revision` | attendance | Status report revised with different accountability markings |
| `attendance_anomaly` | attendance | Presence without activity, absence without notice, streaks |

Custom detectors: drop a `.py` in `detectors/`. Implement `BaseDetector`. Auto-discovered.

### Analyzer (historical)

```
    SQLite                    Commit DAG                    Scoring
    ══════                    ══════════                    ═══════

    events table ──→  Build DAG from git history     ┌─────────────────┐
                      │                              │ Pass 1: Members │
                      ├── Forward/inverted indices   │  7 invariants   │
                      ├── Fork-point resolution      │  per-file blame │
                      ├── Orphan reconstruction      │  concentration  │
                      │   (deleted branches)         ├─────────────────┤
                      └── Branch provenance          │ Pass 2: PM      │
                                                     │  8 dimensions   │
                           Pair Analysis              │  action scoring │
                           ├── Harary chain detect    └────────┬────────┘
                           ├── Hub deduplication               │
                           └── SNA metrics                     ▼
                                                        Permutation test
                                                        (shuffle identities,
                                                         re-run, p-values)
```

Runs on-anomaly (detector fires), on-schedule (nightly), or on-demand (click "analyze").

### Author Resolution — 3-Source Triangulation

Git branch refs are mutable (deletable, GC-able). When a ref is lost, the author becomes unknown. The pipeline triangulates from 3 independent sources:

```
git_ref (branch pointer)  ──┐
board_linker (first link) ───┤──→ triangulate ──→ (author, method, quality)
commit_author (oldest)    ──┘

3 agree  → corroborated     2 agree  → majority (disputed)
1 only   → single-source    0        → unresolvable
                             all ≠    → conflict (disputed)
```

Resolutions persist to `branch_resolutions` table. On each sweep, degraded resolutions (e.g., `corroborated` → `unresolvable`) fire `evidence_degradation` alerts. Invisible data loss becomes a detectable event.

### Storage & Durability

SQLite. One file. WAL mode. `synchronous=FULL`. Single-writer channel with causal ordering.

- **Write channel**: bounded queue (50K) → single writer coroutine → DB. No locks, no races.
- **Alert fences**: alerts flush all pending events atomically. Causal guarantee: if an alert exists, its triggering events are committed.
- **Periodic flush**: 5-second fallback for quiet periods. Max data loss window: 5 seconds.
- **Crash recovery**: `synchronous=FULL` guarantees committed data survives OS crash / power loss. WAL auto-recovers on restart.
- **Event sourcing**: content-addressable `event_hash` (SHA-256). `INSERT OR IGNORE` deduplicates on replay. Profiles are append-only with monotonic versions.
- **CQRS counters**: in-memory write-side projection for alert breakdowns. Dashboard reads counters, not DB. Hydrated from DB on startup for crash recovery.
- **Sweep dedup**: content-addressable profile hash (SHA-256). Identical profiles skip DB write + permutation test. Hash stored alongside version for tamper detection.

### Dashboard

Quart + Alpine.js + Tailwind + 3d-force-graph (lazy loaded). No build step. SSE push to browser — zero polling.

- Team grid (sorted by alert count, searchable, severity-colored badges)
- Tabbed content area: Alerts, Members, Timeline, Graph
- Live alert feed (filterable by team/severity/category, click → evidence detail modal)
- Alert detail modal (triggering event, related alerts, audit trail)
- Member profiles (risk scores, flags, activity counters, expandable detail)
- Timeline view (chronological multi-source event stream per team)
- 3D relationship graph (force-directed, full-width, interactive rotation/zoom)
- Bottom log bar (SSE-driven structured logs: ingest, detect, sweep events, LRU eviction)
- Runtime config panel (detector toggles, thresholds, sweep settings)

---

## Batch Pipeline

One-shot forensic analysis. Complements the real-time system.

```bash
python -m reconcile                          # Full pipeline (ingest → analyze → forensics → output)
python -m reconcile --phase ingest analyze   # Specific phases
python -m reconcile --verify                 # Evidence manifest only
```

Phases: `ingest/` (git, board, discord, email) → `normalize/` (typed objects, unified timeline) → `analyze/` (DAG, provenance, invariants, scoring, pairs) → `forensics/` (snowflake validation, SMTP concordance, evidence manifest) → `output/` (JSON, markdown, visuals).

---

## Scale

| Metric | Target |
|--------|--------|
| Concurrent teams | 200+ |
| Sprint night burst | 1,000 users, 10K events/sec |
| WebSocket connections | 2 total (board + Discord) |
| Polling | 0 |
| SQLite write throughput | 50K inserts/sec (WAL) |
| Memory | <1GB for 200 live teams |
| Dependencies | 4 pip packages |

---

## Repository Structure

```
reconcile/
├── schema.py                    Event, Alert, Category, composite_score
├── bus.py                       EventBus: priority queues, AlertCounters (CQRS)
├── analyzer.py                  Historical profiler: sweep, scores, direction
├── storage.py                   Write channel, alert fences, WAL, synchronous=FULL
├── orchestrator.py              Wires ingestors → bus → detectors → store → dashboard
├── pipeline.py                  Batch pipeline runner (ingest → analyze → output)
├── main.py                      Live CLI (--serve / --live / --sweep)
├── __main__.py                  Batch CLI (python -m reconcile)
├── config_template.py           All configuration knobs
├── ARCHITECTURE.md              Full system architecture (700+ lines)
│
├── ingestors/                   Real-time push ingestors
│   ├── ws_board.py              Board WebSocket (generic, injectable)
│   └── git_poll.py              Git polling ingestor
│
├── ingest/                      Batch data loading
│   ├── git.py, board.py         Git commits/branches, board events/cards
│   ├── discord.py, email.py     Discord messages, status reports
│   └── snapshot.py              Git snapshot capture
│
├── analyze/                     Batch cross-referencing
│   ├── dag.py, provenance.py    Commit DAG, branch ancestry
│   ├── invariants.py            7 parameterized checks
│   ├── scoring.py, pairs.py     Member scoring, pair analysis
│   └── discord.py               Message classification
│
├── forensics/                   Evidence verification
│   ├── snowflake.py             Discord snowflake validation
│   ├── manifest.py              Evidence manifest generation
│   └── smtp.py, consent.py      SMTP concordance, digital consent
│
├── detectors/                   Real-time anomaly detectors (8, auto-discovered)
├── outputs/                     Real-time alert outputs (console, JSONL)
├── output/                      Batch output (JSON, markdown, visuals)
├── provisioning/                Team/Discord auto-setup
│
├── web/
│   ├── app.py                   Quart: REST, webhook, config PATCH
│   ├── sse.py                   SSE: alerts + metrics (CQRS counters) + logs
│   └── templates/
│       └── dashboard.html       Alpine.js + Tailwind + 3d-force-graph
│
└── tests/                       132+ tests (unit, integration, durability, stress)
```

---

## Extending

### Add a source

Write an ingestor. It connects to a push source, normalizes events into `schema.Event`, and emits to the bus. That's it. Everything downstream is source-agnostic.

```python
# ingestors/ws_jira.py
class JiraIngestor(BaseIngestor):
    async def connect(self):
        # Jira webhook or polling adapter
        ...
    
    def normalize(self, raw: dict) -> Event:
        return Event(
            team_id=self.resolve_team(raw["project"]["key"]),
            source="jira",
            kind=raw["webhookEvent"],  # "jira:issue_updated", etc.
            actor=raw["user"]["displayName"],
            entity_id=raw["issue"]["key"],
            detail=raw["issue"]["fields"]["summary"],
            ts=parse_timestamp(raw["timestamp"]),
            raw=raw,
        )
```

### Add a detector

Drop a file in `detectors/`. Implement `BaseDetector`. It receives events from the bus and emits alerts. Auto-discovered at startup.

```python
# detectors/late_night_commit.py
class LateNightCommit(BaseDetector):
    """Flag commits between 2-5 AM local time."""
    
    def check(self, event: Event) -> Alert | None:
        if event.kind == "push" and 2 <= event.ts.hour < 5:
            return Alert(
                detector="late_night_commit",
                severity="info",
                team_id=event.team_id,
                detail=f"{event.actor} committed at {event.ts.strftime('%H:%M')}",
            )
```

---

## License

MIT
