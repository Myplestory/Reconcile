"""SSE (Server-Sent Events) helpers for live dashboard streams."""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator


async def alert_stream(orchestrator) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE events from all teams' alert buses.

    Each event is: data: {json}\n\n
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
    orchestrator.subscribe_alerts(queue)
    try:
        while True:
            alert = await queue.get()
            data = {
                "detector": alert.detector,
                "severity": alert.severity,
                "category": alert.category,
                "score": alert.score,
                "title": alert.title,
                "detail": alert.detail,
                "team_id": alert.team_id,
                "timestamp": alert.timestamp.isoformat(),
                "metadata": alert.metadata,
            }
            yield f"data: {json.dumps(data, default=str)}\n\n"
    finally:
        orchestrator.unsubscribe_alerts(queue)


async def metrics_stream(orchestrator, interval: float = 5.0) -> AsyncGenerator[str, None]:
    """Async generator yielding system metrics every N seconds.

    Pushes: team count, per-team queue depths, uptime, alert counts.
    """
    start_time = time.monotonic()
    while True:
        teams = {}
        for tid, runner in orchestrator.teams.items():
            # CQRS: read from write-side counters (zero DB cost)
            alert_breakdown = runner.bus.alert_counters.snapshot()

            teams[tid] = {
                "name": runner.config.team_name,
                "status": "running" if runner.bus._running else "stopped",
                "queue_depths": runner.bus.queue_depths,
                "timeline_size": len(runner.bus.timeline),
                "detectors": len(runner.bus._detectors),
                "alerts": alert_breakdown,
            }
        data = {
            "uptime_seconds": round(time.monotonic() - start_time, 1),
            "team_count": len(teams),
            "teams": teams,
        }
        yield f"data: {json.dumps(data, default=str)}\n\n"
        await asyncio.sleep(interval)


async def log_stream(orchestrator) -> AsyncGenerator[str, None]:
    """Async generator yielding structured log entries from all teams' buses.

    Backfills from DB on connect, then subscribes for live entries via orchestrator
    (handles teams added after SSE connection).
    LRU eviction and auto-scroll handled client-side.
    """
    # Backfill persisted logs from DB
    if orchestrator._store:
        try:
            historical = await orchestrator._store.read_logs(limit=200)
            for entry in historical:
                yield f"data: {json.dumps(entry, default=str)}\n\n"
        except Exception:
            pass  # DB not ready yet

    # Subscribe for live entries (orchestrator-level, handles dynamic teams)
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    orchestrator.subscribe_logs(queue)
    try:
        while True:
            entry = await queue.get()
            # Filter: only stream detections, system events, and warnings+
            level = entry.get("level", "info")
            source = str(entry.get("source", ""))
            if level in ("warn", "warning", "error", "critical") or \
               source.startswith(("detect.", "watchdog", "sweep", "system", "analyzer")):
                yield f"data: {json.dumps(entry, default=str)}\n\n"
    finally:
        orchestrator.unsubscribe_logs(queue)
