# NLI Classification System Architecture

Source: [`reconcile/analyze/commit_classifier.py`](../analyze/commit_classifier.py) (to be created)

For methodology, model selection rationale, and academic citations, see
[`docs/methodology.md`](methodology.md). For research hypotheses, validation plan,
and threats to validity, see [`docs/hypothesis.md`](hypothesis.md).

---

## 1. Design Paradigm: Hot/Cold Path Classification

The system operates on a **deterministic-baseline-plus-NLI-enhancement** paradigm.
The deterministic baseline (keyword matching, diff file categories, CC prefix parsing,
diff size heuristic) ALWAYS runs and ALWAYS produces a classification. NLI is an optional
enhancement that adds probabilistic confidence when the model is available and confident.
The system produces identical output with or without NLI for any input where deterministic
signals are sufficient.

The two data streams have different latency profiles and use a shared inference engine.
This is a standard pattern in real-time ML serving: separate the latency-critical path
from the throughput-critical path, share the model, serialize GPU access.

### 1.1 Hot Path (Commits)

**Trigger**: `commit.push` events via WebSocket or git poll ingestor.

**Latency contract**: Classification available within one sweep cycle (~30s debounce +
inference time). Sprint-night burst traffic: up to 1,000 commits across 50 teams in a
2-hour window.

**Flow**:
```
WebSocket: commit.push event arrives
    ↓
EventBus.publish() → high-priority queue (< 1ms)
    ↓
Per-event detectors fire via asyncio.gather (< 10ms)    ── NO NLI HERE
    ↓
Detector emits Alert → schedule_debounced_sweep(team_id)
    ↓
30s debounce timer (resets on new events → accumulates sprint-night burst)
    ↓
_run_sweep(team_id)
    ↓
sweep_collaboration() calls CommitClassifier.classify_batch(new_commits)
    ↓
┌─ Cache lookup: per-commit SHA ──────────────────────────────┐
│  HIT  → return cached classification (0ms)                  │
│  MISS → continue to classification pipeline                 │
└─────────────────────────────────────────────────────────────┘
    ↓
Stage 1: Canonicalize (parse CC prefix, strip file refs, enrich with diff metadata)
    ↓
Stage 2: DETERMINISTIC CLASSIFICATION (always runs)
    ├─ CC prefix → category (authoritative when present)
    ├─ Keyword regex on message → category (when matched)
    ├─ Diff file categories via categorize_file()
    └─ Diff size heuristic
    → Produces: deterministic_result (always available)
    → Also records: keyword_only_result (for calibration)
    ↓
┌─ Degenerate check ─────────────────────────────────────────┐
│  < 4 words or GitHub default → USE deterministic_result     │
│  NOT cached (re-scored on next healthy sweep)               │
└─────────────────────────────────────────────────────────────┘
    ↓
Stage 3: NLI scoring (optional enhancement)
    ↓
┌─ Circuit breaker check ────────────────────────────────────┐
│  OPEN → return None immediately (0ms)                       │
│         → USE deterministic_result, NOT cached              │
│  CLOSED/HALF_OPEN → proceed to GPU                          │
└─────────────────────────────────────────────────────────────┘
    ↓
┌─ GPU semaphore (asyncio.Semaphore(1)) ─────────────────────┐
│  Acquire with 30s timeout (congestion, NOT circuit failure) │
│  Timeout → return None → USE deterministic_result           │
│  Acquired → proceed to forward pass                         │
└─────────────────────────────────────────────────────────────┘
    ↓
Forward pass in ThreadPoolExecutor (10s timeout)
    ↓
┌─ Timeout/Exception ────────────────────────────────────────┐
│  → circuit.record_failure()                                 │
│  → return None → USE deterministic_result, NOT cached       │
│  After 3 consecutive failures → circuit OPEN (60s recovery) │
└─────────────────────────────────────────────────────────────┘
    ↓
Stage 4: Multi-signal fusion (NLI + CC prefix + diff + keyword + size)
    ├─ NLI available + confident → fuse all 5 signals
    └─ NLI unavailable → USE deterministic_result (already computed)
    ↓
Record both: fused_result + keyword_only_result (calibration)
    ↓
Cache by commit SHA (NLI-backed only; deterministic NOT cached)
    ↓
Return to sweep → metrics computed with classified commits
    ↓
SSE push to dashboard
```

### 1.2 Cold Path (Cards)

**Trigger**: `card.create` or `card.update` events.

**Latency contract**: Background processing. Minutes acceptable. No burst traffic —
cards are created at sprint planning, not sprint night.

**Flow**:
```
EventBus: card.create event arrives
    ↓
Extract card_name (title) from event.raw + any addComment text
    ↓
Cache lookup: (card_id, title_hash)
    HIT  → return cached (0ms)
    MISS → continue
    ↓
Canonicalize card title (strip noise, flag degenerate)
    ↓
NLI scoring against CARD_HYPOTHESES (different templates from commit path)
    ↓
Uses same InferenceEngine but SEPARATE circuit breaker
    ↓
Cache result by (card_id, title_hash)
```

Card deterministic signals (when NLI unavailable):
- Title keywords: "fix", "implement", "research", "test", "bug", "setup"
- Pipeline column at classification time (card in "Testing" → likely test-related)
- Comment keywords from addComment events

Card deterministic classifications are marked `source: "heuristic-weak"` (weaker than
commit deterministic which has diff metadata). Cross-reference weights card-side
confidence lower when source is heuristic-weak.

**Why separate circuit breakers**: A slow-path failure (e.g., scoring a batch of card
titles during a background sweep) must not trip the hot-path circuit. Sprint-night
commit classification is latency-critical; card classification is not. Independence
prevents cross-contamination.

### 1.3 Cross-Reference

**Trigger**: After both hot and cold paths have classified a commit and its linked card.

**Flow**:
```
For each card with linked commits:
    ↓
Collect commit classifications → majority vote → effective execution type
    ↓
Compare card intent (cold path) vs execution type (hot path aggregate)
    ↓
Report: agreement rate, divergence flags, linkage rate
```

No NLI involved — pure comparison of cached classifications.

---

## 2. InferenceEngine

Shared singleton, initialized at `Orchestrator.start()`. Manages model lifecycle,
GPU access, and circuit-breaking.

### 2.1 Lifecycle

```python
class InferenceEngine:
    """Manages DeBERTa-v3-base lifecycle for NLI scoring.

    Singleton per orchestrator. Shared across all teams.
    All inference runs in ThreadPoolExecutor — event loop never blocks.
    
    Adapted from PolyEdge CrossEncoder (semantic_pipeline/matching/cross_encoder.py):
    - Eager load at startup (not lazy on first request)
    - Device auto-detection (cuda → mps → cpu)
    - torch.compile() with platform guards
    - GPU semaphore for concurrent team serialization
    - Circuit breaker for fail-close behavior
    """
```

**Initialization order** (adapted from PolyEdge `orchestrator/core.py` lines 197–369):

```
Orchestrator.start()
    ↓
1. Store.start_writer()        — DB ready
2. InferenceEngine.initialize() — model loaded, warm-up pass complete
3. Wire teams + buses           — events can now flow
4. Start WebSocket ingestors    — traffic begins
```

Model loads BEFORE any WebSocket connection opens. OOM or driver errors surface at
startup, not on the first sprint-night commit. If `initialize()` fails, the circuit
breaker is forced OPEN — all classifications route to deterministic for the entire
session. The engine is never in a partially-loaded state.

**Warm-up**: A dummy forward pass (`[("test", "This change adds new functionality.")]`)
triggers `torch.compile()` JIT compilation. Without warm-up, the first real batch would
incur a 5–15s compilation penalty, adding unpredictable latency to the first sweep.

### 2.2 Device Auto-Detection

Adapted from PolyEdge `embedding/encoder.py` lines 62–70:

```
Priority: CUDA → MPS → CPU

CUDA:  torch.cuda.is_available()
MPS:   torch.backends.mps.is_available() (macOS Metal Performance Shaders)
CPU:   fallback (always available)
```

Logged at startup: `"NLI engine ready: MoritzLaurer/deberta-v3-base-zeroshot-v2.0 on mps"`

### 2.3 torch.compile() Platform Guards

Adapted from PolyEdge `cross_encoder.py` lines 250–326:

| Platform | torch.compile() | Reason |
|---|---|---|
| CUDA | Enabled (`reduce-overhead` mode) | 1.5–2x speedup, zero accuracy loss |
| MPS (macOS) | Disabled | Metal shader compilation errors (known PyTorch issue) |
| Windows | Disabled | Requires MSVC cl.exe not in standard PATH |
| CPU | Disabled | No benefit at this batch size; compilation overhead wasted |

### 2.4 GPU Semaphore

```python
self._gpu_semaphore = asyncio.Semaphore(1)
```

**Purpose**: Serialize forward passes across concurrent team sweeps. Without
serialization, concurrent sweeps would accumulate GPU activations → OOM.

**Concurrency = 1**: PolyEdge uses `gpu_concurrency=1` as default for the same reason
(see `config.py` line 179, `cross_encoder.py` line 94). Higher concurrency adds VRAM
from overlapping activations without proportional throughput gain on single-GPU systems.

**Semaphore timeout (30s) is separate from inference timeout (10s)**:

Sprint-night scenario: 30 teams queue for GPU. Waiting for other teams to finish
is **congestion**, not failure. The semaphore timeout returns `None` (deterministic
fallback) without tripping the circuit breaker. Only inference timeouts (actual
forward pass exceeded 10s) constitute real failures that trip the circuit.

---

## 3. Circuit Breaker

Adapted from PolyEdge `extraction/circuit_breaker.py`. Standard three-state pattern
per Nygard (2007) *Release It!*.

### 3.1 State Machine

```
    ┌──────────────────────────────────┐
    │           CLOSED                 │
    │   (normal operation, NLI active) │
    └──────────┬───────────────────────┘
               │ 3 consecutive inference failures
               ▼
    ┌──────────────────────────────────┐
    │            OPEN                  │
    │ (NLI bypassed, deterministic)    │──── all score_batch() → None immediately
    └──────────┬───────────────────────┘
               │ 60s elapsed since last failure
               ▼
    ┌──────────────────────────────────┐
    │         HALF_OPEN                │
    │   (test: allow 1 request)        │
    └──────┬───────────┬───────────────┘
           │           │
     success ×2    any failure
           │           │
           ▼           ▼
        CLOSED       OPEN
```

### 3.2 Fail-Close Semantics

**Fail-close** in this context means: on any failure, the system falls back to the
deterministic baseline — it does not halt, hang, return empty, or degrade silently.
This is analogous to a fail-safe in hardware: the safe state is deterministic
classification, not no classification. (Note: this differs from the security usage
of "fail-close" meaning "deny by default.") At no point does the dashboard show
"unknown", "pending", or empty classifications. Every commit always has a classification —
the question is whether NLI or deterministic produced it.

| Failure mode | Behavior | User impact |
|---|---|---|
| `torch` not installed | `NLI_AVAILABLE=False`, keyword-only for session | Lower accuracy on ambiguous commits |
| Model download fails | `initialize()` catches, circuit forced OPEN | Same — deterministic |
| OOM on first forward pass | Circuit records failure, opens after 3 | First 3 sweeps slow (timeout), then instant deterministic |
| GPU driver crash mid-session | `score_batch()` catches, circuit opens | 60s recovery, deterministic during outage |
| Single inference timeout (10s) | Returns None for that sweep | That sweep deterministic. Circuit stays CLOSED (needs 3) |
| Sustained timeouts | Circuit opens after 3 consecutive | All teams deterministic. Recovery tested after 60s |
| Semaphore congestion (30s) | Returns None, NOT a circuit failure | Deterministic for that team, no circuit impact |

### 3.3 Why NOT Retry

Retrying within the same sweep adds latency to a latency-critical path. If the GPU
hung on the first attempt, it will hang on the second. The circuit breaker handles
recovery across sweeps (60s window), not within a single sweep. This is the standard
pattern in ML serving: fail fast, serve stale/fallback, recover asynchronously.

### 3.4 Thread Safety

Circuit breaker methods (`allow_request`, `record_success`, `record_failure`) are
plain attribute mutations. They are NOT protected by a lock.

**This is safe because**: All calls originate from `score_batch()`, which is an `async`
method called from the asyncio event loop (single-threaded). The ThreadPoolExecutor runs
only `_score_batch_sync`, which has no reference to the circuit breaker. No thread ever
calls circuit methods.

This invariant is structural (enforced by the architecture), not accidental. If the
architecture ever changes to allow multi-loop or multi-thread access to the circuit
breaker, a lock must be added.

### 3.5 Two Independent Circuit Breakers

```python
self._hot_circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
self._cold_circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=120.0)
```

- Hot path (commits): 60s recovery — sprint night needs fast recovery
- Cold path (cards): 120s recovery — no time pressure, can wait longer

Independence ensures that a cold-path batch failure during background processing
does not degrade hot-path sprint-night classification.

---

## 4. Caching

### 4.1 Hot Path Cache

- **Key**: `(team_id, commit_sha)` in `git_cache` SQLite table
- **Value**: classification result JSON (category, confidence, scores, signals)
- **Hit policy**: exact match → return cached, skip NLI entirely
- **Write policy**: only NLI-backed classifications are cached
- **Deterministic fallbacks are NOT cached**: if NLI was unavailable (circuit open,
  timeout, degenerate), the result is not written to cache. On the next healthy sweep,
  uncached commits are re-scored with NLI.

**Rationale**: Deterministic classifications are correct but less precise. Caching them
would permanently lock in lower accuracy for commits classified during an outage.
Not caching them allows eventual NLI accuracy when the circuit recovers. The cost of
repeated deterministic classification is negligible (regex + lookup, no GPU).

### 4.2 Cold Path Cache

- **Key**: `(card_id, title_hash)` — title_hash is SHA-256 of the card title text
- **Value**: classification result JSON
- **Invalidation**: if card title changes (`card.update` event), title_hash changes →
  cache miss → re-classify

### 4.3 Cross-Team Sharing

If two teams fork the same repository, shared commits have the same SHA. A cache hit
on `(team_a, sha123)` does not serve `(team_b, sha123)` — teams are isolated. This is
conservative: the same commit in different team contexts may be classified differently
due to different diff metadata or file categories.

---

## 5. Non-Blocking Guarantees

### 5.1 Event Loop Protection

The asyncio event loop must never block. All CPU-intensive work runs in executors:

| Operation | Execution | Why |
|---|---|---|
| Model forward pass | `run_in_executor(None, _score_batch_sync)` | CPU/GPU bound, 200ms–3s |
| Model loading | `run_in_executor(None, _load_model_sync)` | I/O + CPU, 5–15s |
| Tokenization | Inside `_score_batch_sync` (already in executor) | CPU bound, ~10ms |
| Circuit breaker check | Inline on event loop | O(1), ~1µs |
| Cache lookup | SQLite (via async store) | Already async |
| Canonicalization | Inline on event loop | Regex + string ops, ~100µs |

### 5.2 Timeout Hierarchy

```
Semaphore acquire     ← 30s timeout (congestion patience)
    ↓                    timeout → None, NOT circuit failure
Forward pass          ← 10s timeout (inference ceiling)
    ↓                    timeout → None, IS circuit failure
Event loop            ← never blocked (executor isolates)
```

### 5.3 Executor Task Non-Cancellation

`asyncio.wait_for()` raises `TimeoutError` to the awaiting coroutine, but
`run_in_executor` tasks are NOT cancellable — the underlying thread continues
running to completion. On inference timeout:

1. `score_batch()` returns `None` immediately (event loop unblocked)
2. Orphaned thread continues forward pass to completion (GPU still busy)
3. Result is discarded when thread finishes
4. Semaphore released in `finally` block after thread completes

**Impact**: After a timeout, the GPU is occupied until the orphaned forward pass
finishes. The next team's sweep waits on the semaphore until the orphaned thread
releases it. This is a known limitation of Python's executor model, not a bug.

**Mitigation** (implemented if observed in practice): cancellation flag checked
between micro-batches. Micro-batch size = 64 pairs. Forward pass of 160 pairs =
3 micro-batches. Cancel takes effect at the next boundary (~200ms GPU, ~2.5s CPU
worst case).

---

## 6. Scoring Pipeline Detail

### 6.1 Stage 1: Canonicalization

**Input**: raw commit message + diff metadata  
**Output**: `CanonicalCommit(prefix, body, enrichment, degenerate, raw)`

1. **Parse Conventional Commits prefix** (if present)
   ```
   "fix(auth): resolve reset password" → prefix="fix", scope="auth", body="resolve reset password"
   ```
   Regex: `^(feat|fix|refactor|docs|chore|style|test|perf|ci|build|revert)(\(.+?\))?[!]?:\s*(.+)`

2. **Strip file references** (noise for entailment scoring)
   ```
   "drivers.php, patch endpoint added for orders.php" → "patch endpoint added"
   ```

3. **Enrich with diff metadata** (observable facts)
   ```
   "Modified 3 files in backend:api and frontend:page (+45 -12)"
   ```
   Uses existing `categorize_file()` on each changed file.

4. **Flag degenerate** (<4 words after stripping, GitHub defaults, generic patterns)

**Final canonical text** (what the model sees):
```
"Corrected case-sensitive import for password reset. Modified 1 file in frontend:page (+3 -3)."
```

### 6.2 Stage 2: Hypothesis Pairing

For each non-degenerate commit, form 8 pairs:
```python
pairs = [(canonical_text, hypothesis) for hypothesis in HYPOTHESES.values()]
```

All 8 scored in ONE micro-batch (or split across micro-batches of 64 for multiple commits).

### 6.3 Stage 3: NLI Scoring

Direct tokenizer + `model.forward()` (adapted from PolyEdge `cross_encoder.py`
lines 464–550). NOT HuggingFace `pipeline()` — pipeline's Dataset/DataLoader overhead
causes 2–3x regression on small batches.

```python
inputs = tokenizer(premises, hypotheses, padding=True, truncation=True,
                   max_length=256, return_tensors="pt").to(device)
with torch.no_grad():
    logits = model(**inputs).logits
    probs = torch.softmax(logits, dim=-1)
```

Output per pair: `{entailment: float, not_entailment: float}` (binary — zeroshot-v2.0 collapses neutral+contradiction)

### 6.4 Stage 4: Multi-Signal Fusion

Weighted combination with redistribution when signals are absent
(adapted from PolyEdge `pair_verifier.py`):

```
Available signals:           Weights:
  NLI (when confident)         0.40
  CC prefix (when present)     0.25
  Diff file categories         0.20
  Keyword regex                0.10
  Diff size heuristic          0.05

Redistribution examples:
  No CC prefix:    NLI=0.52, diff=0.27, keyword=0.13, size=0.08
  No NLI:          CC=0.42, diff=0.33, keyword=0.17, size=0.08
  Neither:         diff=0.57, keyword=0.29, size=0.14  (pure deterministic)
```

Each missing signal's weight is redistributed proportionally to remaining signals.
The system always produces a classification — there is no "unknown" state.

Every classification records both the fused result and the keyword-only result:

```python
{
    'classification': 'feature',           # fused result (final)
    'classification_deterministic': 'unknown',  # deterministic-only for same input
    'nli_contributed': True,               # did NLI change the outcome?
    'agreement': False,                    # did deterministic agree with fused?
}
```

This enables the calibration report ([`hypothesis.md`](hypothesis.md) §7.7) to measure
NLI's marginal contribution empirically.

---

## 7. Integration Points

### 7.1 Orchestrator (`reconcile/orchestrator.py`)

```python
# At Orchestrator.start():
if NLI_AVAILABLE:
    engine = InferenceEngine(
        model_name="MoritzLaurer/deberta-v3-base-zeroshot-v2.0",
        batch_size=64,
        inference_timeout=10.0,
        semaphore_timeout=30.0,
    )
    await engine.initialize()
    self._commit_classifier = CommitClassifier(engine=engine)
else:
    self._commit_classifier = CommitClassifier(engine=None)
```

### 7.2 Analyzer (`reconcile/analyzer.py`)

```python
# In sweep_collaboration():
if self._commit_classifier:
    classifications = await self._commit_classifier.classify_batch(new_commits)
    # Merge into commit analysis for metric segmentation
```

### 7.3 Board Ingestor (`reconcile/ingestors/ws_board.py`)

```python
# In _extract_metadata() for card.create / card.update:
metadata["card_name"] = raw.get("card_name", "")
# Makes card title available to cold path without additional API call
```

### 7.4 Storage (`reconcile/storage.py`)

Uses existing `git_cache` table:
```sql
CREATE TABLE IF NOT EXISTS git_cache (
    id INTEGER PRIMARY KEY,
    team_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,     -- commit SHA or "card:{card_id}:{title_hash}"
    computed_at TEXT NOT NULL,
    data JSON NOT NULL,
    UNIQUE(team_id, cache_key)
);
```

---

## 8. Compute Bounds

### 8.1 Memory (Hard Ceiling)

| Component | Size | Notes |
|---|---|---|
| Model weights (FP32) | 440MB | Loaded once, shared across all teams |
| Peak activations (batch=64, seq=256) | ~360MB | One forward pass at a time (semaphore) |
| Tokenizer vocabulary | ~5MB | In-memory hash table |
| Classification cache | ~2MB | SQLite rows, not in-memory |
| **TOTAL PEAK** | **~805MB** | **Constant regardless of team count** |

### 8.2 Latency

| Scenario | GPU (compiled) | GPU | MPS | CPU |
|---|---|---|---|---|
| Per micro-batch (64 pairs) | ~130ms | ~200ms | ~500ms | ~2.5s |
| Per team (20 commits × 8 hyp = 160 pairs) | 0.3s | 0.5s | 1.2s | 6.2s |
| Sprint night (50 teams, serialized) | 16s | 25s | 62s | 312s |
| Sprint night (10-team staggered windows) | 3s/window | 5s/window | 12s/window | 62s/window |
| Cache hit | 0ms | 0ms | 0ms | 0ms |

### 8.3 Semester Total

| Path | Pairs | GPU time | CPU time | Cloud cost (T4 @ $0.50/hr) |
|---|---|---|---|---|
| Hot (commits) | 80,000 | 2.6 min | 52 min | $0.02 |
| Cold (cards) | 40,000 | 1.3 min | 26 min | $0.01 |
| **Total** | **120,000** | **3.9 min** | **78 min** | **$0.03** |

---

## 9. Dependencies

```
# requirements.txt additions (OPTIONAL — engine works without these)
torch>=2.0.0
transformers>=4.35.0
```

No `bitsandbytes` (no quantization). No `sentence-transformers` (no embedding).
No `qdrant-client` (no vector store).

If neither `torch` nor `transformers` is installed:
- `NLI_AVAILABLE = False`
- `CommitClassifier(engine=None)` uses keyword + diff deterministic mode
- All other engine functionality (metrics, detectors, dashboard) is unaffected
- The NLI pipeline is purely additive — removing it degrades accuracy, not functionality

---

## 10. File Manifest

| File | Action | Purpose |
|---|---|---|
| `reconcile/analyze/commit_classifier.py` | Create | InferenceEngine, CircuitBreaker, CommitClassifier, canonicalization, fusion |
| `reconcile/analyze/code_quality.py` | Modify | Replace `classify_commit()` calls with CommitClassifier |
| `reconcile/orchestrator.py` | Modify | Initialize InferenceEngine at startup, pass to analyzer |
| `reconcile/analyzer.py` | Modify | Accept CommitClassifier, call in sweep path |
| `reconcile/ingestors/ws_board.py` | Modify | Extract card_name into event metadata |
| `requirements.txt` | Modify | Add torch, transformers (optional) |
| `reconcile/docs/methodology.md` | Modify | Apply 13 audit amendments (citations, reframing, validation §8) |
| `reconcile/docs/hypothesis.md` | Create | Research hypotheses, validation plan, threats to validity |
| `reconcile/docs/system.md` | Modify | Apply 7 amendments (deterministic-first framing, calibration, fail-close) |
