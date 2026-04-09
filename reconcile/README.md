# Reconcile

**Collaboration health monitoring engine for software engineering teams.**

Ingests project board activity, git history, and status reports. Classifies
work artifacts via zero-shot NLI + deterministic heuristics. Computes
collaboration metrics (Gini, entropy, bus factor, churn) segmented by work
type. Surfaces team health through a live dashboard.

Single process. Single event loop. Fully async. Handles 200+ teams.

---

## Quick Start

```bash
pip install -r requirements.txt

# Optional: download NLI model for enhanced classification (~440MB)
python scripts/download_models.py --verify

# Configure team
cp reconcile/config_template.py reconcile/config_local.py
# Edit config_local.py: team name, member map, git repo path

# Start
python -m reconcile.main --serve
# Dashboard at http://localhost:8080
```

Load historical data:
```bash
curl -X POST localhost:8080/api/teams/<team_id>/replay
curl -X POST localhost:8080/api/teams/<team_id>/collaboration/compute
```

---

## Architecture

```
Ingestors                     Event Bus                  Analysis
═════════                     ═════════                  ════════

┌─ Board WebSocket ──┐
│  (PMTool, Jira)    │──┐     ┌──────────────┐
├─ Git Poll ─────────┤  │     │ Priority     │     Detectors (9)
│  (any local repo)  │──┤ ──→ │ Queues       │ ──→ Anomaly alerts
├─ Email IMAP ───────┤  │     │ Batch Drain  │
│  (status reports)  │──┘     └──────┬───────┘     Analyzer
│                    │               │         ┌── Collaboration metrics
└────────────────────┘               │         ├── NLI classification
                              ┌──────┴───────┐ ├── Code quality taxonomy
                              │   SQLite     │ └── Member profiles
                              │   (WAL)      │
                              └──────┬───────┘     Dashboard
                                     │         ┌── KPI tiles + trends
                                     ▼         ├── Work type distribution
                               SSE → Browser   ├── Per-member breakdown
                              (Alpine.js +     └── Alert feed + timeline
                               Tailwind +
                               Chart.js)
```

### NLI Classification Pipeline

Deterministic baseline always runs. NLI enhances when available.

```
Commit message → Canonicalize → Deterministic (keyword + diff + CC prefix)
                                      ↓
                               NLI available? → DeBERTa entailment scoring
                                      ↓
                               Multi-signal fusion → Classification
                                      ↓
                               Cache by SHA → Collaboration metrics
```

Three classification paths (priority order):
1. **Cache hit** — prior replay or sweep populated the classifier cache
2. **Full git parse** — `git log -p` → diff-aware classification (most precise)
3. **Event-based** — commit message only, no diffs (works without git repo)

Circuit breaker (Nygard 2007) routes to deterministic on NLI failure.

### Collaboration Metrics

| Metric | Citation | What it measures |
|--------|----------|------------------|
| Gini coefficient | Mockus et al. 2002 | Work distribution inequality |
| Shannon entropy | Hassan 2009 | Participation breadth |
| Bus factor | Avelino et al. 2016 | Knowledge concentration risk |
| Deadline clustering | Eyolfson et al. 2011 | Sprint cramming ratio |
| Commit cadence | Claes et al. 2018 | Engagement regularity |
| Churn balance | Nagappan & Ball 2005 | Code stability |

Three-tier view: Git (code, PM excluded) / Board (PM/coordination) / Combined.

---

## Repository Structure

```
reconcile/
├── schema.py                 Event, Alert, priority model
├── bus.py                    EventBus: priority queues, backpressure, safe ingestors
├── analyzer.py               Historical sweep: profiles + collaboration metrics
├── storage.py                SQLite WAL, durable writes, crash recovery
├── orchestrator.py           Multi-team async orchestration, NLI engine lifecycle
├── main.py                   CLI (--serve / --live / --sweep)
│
├── analyze/                  Analysis modules
│   ├── commit_classifier.py  NLI + deterministic classification pipeline
│   ├── collaboration.py      Gini, entropy, bus factor, cadence, clustering
│   ├── code_quality.py       Diff taxonomy, rewrite detection, file categorization
│   ├── git_churn.py          Blame snapshot, churn decomposition (async)
│   ├── branch_resolution.py  Unmerged branch classifier (9 resolution states)
│   └── attendance.py         Status report parser
│
├── detectors/                Anomaly detectors (9 built-in, auto-discovered)
├── ingestors/                WebSocket, git poll (graceful 503 deferral)
├── outputs/                  Console, JSONL alert outputs
│
├── web/
│   ├── app.py                Quart REST + SSE endpoints
│   └── templates/
│       └── dashboard.html    Alpine.js + Tailwind + Chart.js
│
├── docs/
│   ├── methodology.md        NLI academic methodology + model selection
│   ├── system.md             System architecture (hot/cold path, circuit breaker)
│   ├── hypothesis.md         Research hypotheses (H1-H3), validation plan
│   ├── pretune-hypothesis-run.md  Initial NLI sweep results (108 commits)
│   ├── api.md                REST + SSE endpoint reference
│   ├── schema.md             Event/Alert schema
│   ├── storage.md            SQLite storage design
│   ├── detectors.md          Detector documentation
│   └── benchmarks.md         Performance benchmarks
│
└── tests/                    286 tests
```

---

## Detectors

| Detector | Watches for |
|----------|-------------|
| `zero_commit_complete` | Card completed with 0 commits on linked branch |
| `branch_delete_complete` | Branch deleted within N seconds before card completed |
| `batch_completion` | N+ cards completed by same actor in rapid succession |
| `file_reattribution` | File re-added byte-identical under different author |
| `completion_non_assignee` | Card completed by someone other than assignee |
| `unrecorded_deletion` | Branch deleted in git with no board record |
| `report_revision` | Status report revised with different markings |
| `attendance_anomaly` | Presence without activity, absence streaks |
| `column_flow` | Complete without testing, backlog regression, non-PM close |

Custom detectors: implement `BaseDetector`, drop in `detectors/`. Auto-discovered.

---

## Documentation

| Document | Purpose |
|----------|---------|
| [docs/methodology.md](docs/methodology.md) | NLI classification methodology, academic citations |
| [docs/system.md](docs/system.md) | System architecture, circuit breaker, caching |
| [docs/hypothesis.md](docs/hypothesis.md) | Research hypotheses, validation plan, threats to validity |
| [docs/api.md](docs/api.md) | REST + SSE endpoint reference |
| [docs/detectors.md](docs/detectors.md) | Detector documentation |

---

## Tests

```bash
python -m pytest reconcile/tests/ -v    # 286 tests
```

---

## License

MIT
