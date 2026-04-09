# Benchmarks

_Generated 2026-04-07 08:32 UTC_

10 iterations per benchmark + 2 warm-up (discarded).

## 1. Write Throughput

Events enqueued → channel → writer → SQLite. Includes channel overhead + DB commits.

| Events | Batch Size | Mean (events/s) | Stddev | Min | Max | N |
|-------:|:----------:|----------------:|-------:|----:|----:|--:|
| 10,000 | 50 | 48.6K | 1.5K | 46.2K | 50.3K | 10 |
| 10,000 | 200 | 65.5K | 1.0K | 63.8K | 66.9K | 10 |
| 10,000 | 500 | 73.1K | 691 | 71.7K | 73.8K | 10 |
| 100,000 | 50 | 9.1K | 38 | 9.0K | 9.2K | 10 |
| 100,000 | 200 | 9.1K | 56 | 9.0K | 9.2K | 10 |
| 100,000 | 500 | 9.1K | 28 | 9.1K | 9.2K | 10 |

## 2. Alert Fence Latency (ms)

Time from `enqueue_alert()` to data visible in DB. Includes async yield overhead (~0.1ms floor).

### Idle (50 events/fence)

| Mean | Stddev | P50 | P99 | Min | Max | N |
|-----:|-------:|----:|----:|----:|----:|--:|
| 0.62 | 0.25 | 0.57 | 1.67 | 0.37 | 3.46 | 1000 |

### Saturated (full batch pending before fence)

**Batch size 100:**

| Mean | Stddev | P50 | P99 | Max | N |
|-----:|-------:|----:|----:|----:|--:|
| 1.02 | 0.21 | 1.00 | 1.66 | 3.79 | 300 |

**Batch size 500:**

| Mean | Stddev | P50 | P99 | Max | N |
|-----:|-------:|----:|----:|----:|--:|
| 3.02 | 1.04 | 2.88 | 8.86 | 13.96 | 300 |

## 3. Read Concurrency Under Write Load (ms)

Read latency with N concurrent readers during 5K active writes + fences.

**1 reader(s):**

| Mean | Stddev | P50 | P99 | Max | N |
|-----:|-------:|----:|----:|----:|--:|
| 1.73 | 4.84 | 0.33 | 19.11 | 47.83 | 300 |

**5 reader(s):**

| Mean | Stddev | P50 | P99 | Max | N |
|-----:|-------:|----:|----:|----:|--:|
| 1.93 | 4.69 | 0.62 | 21.87 | 23.42 | 1500 |

**10 reader(s):**

| Mean | Stddev | P50 | P99 | Max | N |
|-----:|-------:|----:|----:|----:|--:|
| 1.84 | 4.39 | 0.66 | 21.69 | 23.69 | 3000 |

Bimodal distribution: most reads complete in <1ms (p50); occasional reads coincide with WAL checkpoint or writer commit, causing ~20ms spikes. This is expected SQLite WAL behavior.

## 4. Scale Degradation

Write throughput (10K events measured) vs pre-existing DB size. 3 iterations per size.

| Pre-existing | DB Size | Mean (events/s) | Stddev |
|-----------:|--------:|----------------:|-------:|
| 0 | 3,440 KB | 43.2K | 436 |
| 10,000 | 6,800 KB | 38.9K | 830 |
| 50,000 | 20,364 KB | 21.1K | 520 |
| 100,000 | 20,732 KB | 17.7K | 1.7K |

## 5. Sustained Load

Continuous writes for 60s. 5-second window rates.

| Duration | Total Events | Avg Rate | Windows | Stddev | First Window | Last Window |
|:--------:|:------------:|---------:|--------:|-------:|:------------:|:-----------:|
| 60.0s | 5,166,162 | 86.1K | 11 | 1.7K | 87.3K | 85.6K |

## 6. Memory (Python Heap via tracemalloc)

Measures Python allocator only. Excludes SQLite internal allocations.

| Events | Heap Before (KB) | Heap After (KB) | Peak (KB) | Delta (KB) |
|-------:|:----------------:|:---------------:|:---------:|-----------:|
| 10,000 | 1 | 70 | 8,635 | 69 |
| 50,000 | 1 | 22,829 | 43,270 | 22,829 |

## 7. Shutdown Drain (ms)

Time to drain all pending events on `close()`. Verified 100% persisted.

| Events | Mean | Stddev | P50 | Max | N | Persisted |
|-------:|-----:|-------:|----:|----:|--:|:---------:|
| 1,000 | 4.8 | 0.2 | 4.8 | 5.2 | 10 | 100% |
| 10,000 | 62.3 | 1.0 | 62.4 | 64.1 | 10 | 100% |

## 8. Startup (ms)

| Mode | Mean | Stddev | Min | Max | N |
|:-----|-----:|-------:|----:|----:|--:|
| Cold (new DB) | 2.3 | 0.9 | 1.7 | 3.8 | 10 |
| Warm (reopen) | 0.4 | 0.0 | 0.4 | 0.5 | 10 |

---

### Environment

| | |
|---|---|
| CPU | Apple M4, MacBook Pro |
| RAM | 16 GB unified memory |
| OS | macOS Sequoia 15.5 |
| Python | 3.12 |
| SQLite | 3.45+ (via aiosqlite) |

Benchmarks were run on Apple Silicon. Production target is Linux/x86_64; figures are for relative component comparison, not absolute latency guarantees.

### Methodology

| Test | Workload | Iterations | Warm-up |
|------|----------|:----------:|:-------:|
| 1. Write Throughput | 10K–100K events × 3 batch sizes (50, 200, 500) | 10 | 2 discarded |
| 2. Fence Latency (idle) | 100 fences × 50 events = 5K events + 100 alerts | 10 | 2 discarded |
| 2. Fence Latency (saturated) | 30 fences × batch_size events | 10 | 2 discarded |
| 3. Read Concurrency | 5K events + 50 fences, 1/5/10 readers × 30 reads | 10 | 2 discarded |
| 4. Scale Degradation | Pre-populate 0–100K, measure 10K insert rate | 3 per size | cold start |
| 5. Sustained Load | Continuous writes for 60s, fences every 200 events | 1 | none |
| 6. Memory | Ingest 10K/50K events, tracemalloc Python heap | 1 | none |
| 7. Shutdown Drain | Enqueue 1K/10K events, close(), verify persistence | 10 | 2 discarded |
| 8. Startup | New DB (cold) + reopen (warm) | 10 | 2 discarded |

- Fresh temp DB per iteration (no cross-contamination)
- `time.monotonic()` for all timings (monotonic, not wall clock)
- `PRAGMA synchronous=FULL` active (production setting)
- Realistic event payloads (8 action types, variable metadata, skewed actor distribution)
- `tracemalloc` for Python heap measurement (excludes SQLite internals)
- Fence latency includes async yield overhead (~0.1ms floor)
- P99 reported only when N >= 100; otherwise Max only

```bash
python3 scripts/bench.py          # full suite (~3 min)
python3 scripts/bench.py --quick   # smoke test (~1 min)
```
