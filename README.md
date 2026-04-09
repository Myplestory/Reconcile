# Reconcile v2

Real-time collaboration health monitoring engine for software engineering teams.

Ingests project management board activity, git history, and status reports. Computes
collaboration metrics (Gini, entropy, bus factor, churn decomposition) segmented by
work type via zero-shot NLI classification. Surfaces findings through a live web
dashboard with per-sprint trend analysis.

**v2 adds**: NLI-enhanced commit/card classification (DeBERTa-v3-base), hot/cold path
architecture, multi-signal fusion, circuit breaker resilience, and academically
structured hypothesis testing. See [docs/](reconcile/docs/) for methodology.

v1 forensics engine archived in [`v1/`](v1/).

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Optional: download NLI model (~440MB, falls back to keyword+diff if absent)
python scripts/download_models.py --verify

# Start server
python -m reconcile.main --serve
# Open localhost:8080
```

## Architecture

Three layers:

1. **Ingestors** -- WebSocket, git poll, email IMAP. Swappable per source (GitHub, GitLab, Jira, etc.)
2. **Event bus** -- Priority queues, batch drain, pluggable detectors. Durable SQLite WAL writes.
3. **Analyzer** -- Historical sweep with collaboration metrics, NLI classification, trend baselines.

### NLI Classification (v2)

Zero-shot natural language inference classifies commits and board cards by work type
without labeled training data (Yin et al. 2019). Deterministic baseline (keyword +
diff categories + CC prefix) always runs. NLI is an optional enhancement layer with
fail-close circuit breaker.

```
Commit message ──> Canonicalize ──> Deterministic (always) ──> NLI (when available)
                                           |                         |
                                     keyword + diff            DeBERTa entailment
                                           |                         |
                                           └───── Multi-signal fusion ─────> Classification
```

See:
- [`reconcile/docs/methodology.md`](reconcile/docs/methodology.md) -- Academic methodology, model selection
- [`reconcile/docs/system.md`](reconcile/docs/system.md) -- System architecture, circuit breaker, caching
- [`reconcile/docs/hypothesis.md`](reconcile/docs/hypothesis.md) -- Research hypotheses, validation plan
- [`reconcile/docs/pretune-hypothesis-run.md`](reconcile/docs/pretune-hypothesis-run.md) -- Initial NLI sweep results

## Repository Structure

```
reconcile/                  Engine source
  analyze/                  Collaboration metrics, code quality, NLI classifier
    commit_classifier.py    NLI + deterministic classification pipeline
    collaboration.py        Gini, entropy, bus factor, churn, cadence
    code_quality.py         Diff taxonomy, rewrite detection
    git_churn.py            Blame snapshot, churn decomposition
    branch_resolution.py    Unmerged branch classifier
    attendance.py           Status report parser
  analyzer.py               Historical sweep orchestration
  orchestrator.py           Multi-team async orchestration
  bus.py                    Event bus, priority queues, backpressure
  storage.py                SQLite WAL, durable writes, crash recovery
  detectors/                Pluggable anomaly detectors (9 built-in)
  ingestors/                WebSocket, git poll, email IMAP
  web/                      Dashboard (Quart + Alpine.js + Chart.js)
  docs/                     Methodology, system architecture, hypothesis
  tests/                    282 unit tests

scripts/
  download_models.py        NLI model download + verification

v1/                         Archived v1 forensics engine
```

## Tests

```bash
python -m pytest reconcile/tests/ -v    # 282 tests
```

## Configuration

1. Copy `reconcile/config_template.py` to `reconcile/config_local.py`
2. Fill in team details (member map, git repo path, board credentials)
3. Copy `.env.example` to `.env` for API keys

The engine is team-agnostic. Configuration determines which teams to monitor.

## Academic References

- Yin et al. (2019) "Benchmarking Zero-shot Text Classification" EMNLP
- He et al. (2021) "DeBERTa" ICLR
- Mockus & Votta (2000) "Identifying Reasons for Software Changes" ICSM
- Levin & Yehudai (2017) "Boosting Automatic Commit Classification" JSS
- Cataldo et al. (2006) "Identification of Coordination Requirements" CSCW
- Nygard (2007) "Release It!" -- circuit breaker pattern

Full citations in [`reconcile/docs/methodology.md`](reconcile/docs/methodology.md).
