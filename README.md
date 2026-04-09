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
pip install -r requirements.txt

# Optional: download NLI model (~440MB, falls back to keyword+diff if absent)
python scripts/download_models.py --verify

# Configure team
cp reconcile/config_template.py reconcile/config_local.py
# Edit config_local.py

# Start server
python -m reconcile.main --serve
# Dashboard at http://localhost:8080
```

See [reconcile/README.md](reconcile/README.md) for full documentation.

## Tests

```bash
python -m pytest reconcile/tests/ -v    # 286 tests
```
