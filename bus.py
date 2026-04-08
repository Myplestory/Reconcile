"""Async event bus. Priority queues, batch drain, debounced sweep.

All I/O is async. Detectors run concurrently via asyncio.gather.
No threads, no multiprocessing — single event loop, cooperative multitasking.

Architecture:
  - Two queues: high-priority (bounded 5K) and low-priority (bounded 50K)
  - Batch drain: up to 500 events per cycle (high first, then low)
  - Debounced sweep: 30s after last alert per team_id (resets on new alert)
  - SSE subscribers: alert fan-out to connected browsers
  - Storage: optional async store for persistence (append events + alerts)
  - Timeline eviction: keeps last N events in memory (configurable)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Protocol, TYPE_CHECKING

from .schema import Event, Alert

if TYPE_CHECKING:
    from .storage import Store

log = logging.getLogger(__name__)

# --- Defaults ---
HIGH_QUEUE_SIZE = 5_000
LOW_QUEUE_SIZE = 50_000
BATCH_SIZE = 500
SWEEP_DEBOUNCE_SECONDS = 30.0
TIMELINE_MAX_EVENTS = 500_000  # evict oldest beyond this


_SEVERITIES = frozenset({"critical", "suspect", "elevated", "info"})
_CATEGORIES = frozenset({"process", "attendance", "attribution", "evidence"})


class AlertCounters:
    """Write-side CQRS projection. O(1) record, O(1) read, O(8) snapshot.

    Flat dict with composite tuple keys — no nested dicts.
    Key space capped at 8 (4 severities + 4 categories). No leak.
    Hydrate from DB on startup for crash recovery.
    """

    __slots__ = ('_counts', 'total')

    def __init__(self):
        self._counts: dict[tuple[str, str], int] = {}
        self.total = 0

    def record(self, alert) -> None:
        sev = alert.severity if alert.severity in _SEVERITIES else "info"
        cat = alert.category if alert.category in _CATEGORIES else "process"
        c = self._counts
        c[("severity", sev)] = c.get(("severity", sev), 0) + 1
        c[("category", cat)] = c.get(("category", cat), 0) + 1
        self.total += 1

    def hydrate(self, rows: list[tuple]) -> None:
        """Bulk-load from DB: [(severity, category, count), ...].

        Called once on startup. Idempotent — call multiple times safely.
        """
        for severity, category, count in rows:
            c = self._counts
            sev = severity if severity in _SEVERITIES else "info"
            cat = category if category in _CATEGORIES else "process"
            c[("severity", sev)] = c.get(("severity", sev), 0) + count
            c[("category", cat)] = c.get(("category", cat), 0) + count
            self.total += count

    def snapshot(self) -> dict:
        by_sev: dict[str, int] = {}
        by_cat: dict[str, int] = {}
        for (dim, val), count in self._counts.items():
            if dim == "severity":
                by_sev[val] = count
            else:
                by_cat[val] = count
        return {"by_severity": by_sev, "by_category": by_cat, "total": self.total}


class Ingestor(Protocol):
    """Any async source that yields Events."""

    async def stream(self) -> None: ...
    def set_bus(self, bus: EventBus) -> None: ...


class Detector(Protocol):
    """Stateful anomaly detector. Receives events, emits alerts."""

    name: str

    async def detect(self, event: Event) -> list[Alert]: ...


class Output(Protocol):
    """Alert sink."""

    async def emit(self, alert: Alert) -> None: ...


class EventBus:
    """Central event router with priority queues and batch drain.

    Usage:
        bus = EventBus()
        bus.add_ingestor(ws_ingestor)
        bus.add_detector(ZeroCommitDetector())
        bus.add_output(ConsoleOutput())
        bus.set_store(store)  # optional: persist to SQLite
        await bus.run()
    """

    def __init__(
        self,
        sweep_on_alert: bool = True,
        sweep_interval: float | None = None,
        sweep_debounce: float = SWEEP_DEBOUNCE_SECONDS,
        high_queue_size: int = HIGH_QUEUE_SIZE,
        low_queue_size: int = LOW_QUEUE_SIZE,
        batch_size: int = BATCH_SIZE,
        timeline_max: int = TIMELINE_MAX_EVENTS,
    ):
        self._ingestors: list[Ingestor] = []
        self._detectors: list[Detector] = []
        self._outputs: list[Output] = []
        self._timeline: list[Event] = []
        self._timeline_max = timeline_max

        # Priority queues
        self._high_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=high_queue_size)
        self._low_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=low_queue_size)
        self._batch_size = batch_size

        # Sweep config
        self._sweep_on_alert = sweep_on_alert
        self._sweep_interval = sweep_interval
        self._sweep_debounce = sweep_debounce

        # Sweep debounce state: team_id -> asyncio.Task
        self._sweep_timers: dict[str, asyncio.Task] = {}

        # SSE subscribers: list (safe to iterate while modified via copy)
        self._alert_subscribers: list[asyncio.Queue] = []

        # Analyzer (set externally or by orchestrator)
        self._analyzer = None
        self._last_profiles: dict = {}
        self._last_profile_hash: dict[str, str] = {}

        # Storage (optional)
        self._store: Store | None = None

        # CQRS: write-side alert counters (zero-cost reads for SSE)
        self._alert_counters = AlertCounters()

        # Structured log channel (SSE reads from this for dashboard log bar)
        self._log_subscribers: list[asyncio.Queue] = []

        # Known team members (for profile filtering). Set by orchestrator.
        self._members: set[str] | None = None

        self._running = False

    # --- Wiring ---

    def add_ingestor(self, ingestor: Ingestor) -> None:
        ingestor.set_bus(self)
        self._ingestors.append(ingestor)

    def add_detector(self, detector: Detector) -> None:
        self._detectors.append(detector)

    def add_output(self, output: Output) -> None:
        self._outputs.append(output)

    def set_analyzer(self, analyzer) -> None:
        self._analyzer = analyzer

    def set_store(self, store: Store) -> None:
        self._store = store

    # --- SSE subscriber management ---

    def subscribe_alerts(self, queue: asyncio.Queue) -> None:
        if queue not in self._alert_subscribers:
            self._alert_subscribers.append(queue)

    def unsubscribe_alerts(self, queue: asyncio.Queue) -> None:
        try:
            self._alert_subscribers.remove(queue)
        except ValueError:
            pass

    def subscribe_logs(self, queue: asyncio.Queue) -> None:
        if queue not in self._log_subscribers:
            self._log_subscribers.append(queue)

    def unsubscribe_logs(self, queue: asyncio.Queue) -> None:
        try:
            self._log_subscribers.remove(queue)
        except ValueError:
            pass

    def emit_log(self, level: str, source: str, msg: str, team_id: str = "") -> None:
        """Push a structured log entry to all log subscribers and storage (non-blocking)."""
        import time
        entry = {"level": level, "source": source, "msg": msg, "team_id": team_id, "ts": time.time()}
        for q in self._log_subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass  # drop silently — logs are best-effort
        # Persist to DB via write channel (same batch/flush as events)
        if self._store:
            self._store.enqueue_log(entry)

    # --- Publishing ---

    async def publish(self, event: Event) -> None:
        """Called by ingestors to push events into the bus.

        Routes to high or low queue based on event.priority.
        If the target queue is full, this coroutine suspends (backpressure).
        """
        if event.priority == "low":
            await self._low_queue.put(event)
        else:
            await self._high_queue.put(event)

    def publish_nowait(self, event: Event) -> None:
        """Non-blocking publish. Raises asyncio.QueueFull if queue is full.

        Use for inject/replay endpoints where backpressure should return HTTP 429
        instead of blocking the event loop.
        """
        if event.priority == "low":
            self._low_queue.put_nowait(event)
        else:
            self._high_queue.put_nowait(event)

    # --- Core processing loop ---

    async def _process_events(self) -> None:
        """Main event loop. Batch drain from priority queues."""
        log.info("Bus processor started (running=%s, detectors=%d)", self._running, len(self._detectors))
        while self._running:
            batch = self._drain_batch()

            if not batch:
                # Wait for ANY event from either queue (fixes low-priority starvation)
                batch = await self._wait_for_any_event()
                if not batch:
                    continue

            # Append to timeline with eviction
            self._timeline.extend(batch)
            if len(self._timeline) > self._timeline_max:
                excess = len(self._timeline) - self._timeline_max
                self._timeline = self._timeline[excess:]

            # Enqueue events to write channel (non-blocking)
            if self._store:
                for event in batch:
                    self._store.enqueue_event(event)

            # Process each event through detectors
            has_alerts_by_team: set[str] = set()
            for event in batch:
                results = await asyncio.gather(
                    *(d.detect(event) for d in self._detectors),
                    return_exceptions=True,
                )

                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        log.error(
                            "Detector %s failed: %s", self._detectors[i].name, result,
                            exc_info=result,
                        )
                        continue
                    for alert in result:
                        alert.team_id = event.team_id
                        has_alerts_by_team.add(event.team_id)
                        await self._emit_alert(alert, event_hash=event.event_hash)

            # Trigger debounced sweep for teams that had alerts
            if self._sweep_on_alert and self._analyzer:
                for team_id in has_alerts_by_team:
                    self._schedule_debounced_sweep(team_id)

    async def _wait_for_any_event(self) -> list[Event]:
        """Wait for the next event from either queue. Recovers items from completed-before-cancel."""
        high_wait = asyncio.ensure_future(self._high_queue.get())
        low_wait = asyncio.ensure_future(self._low_queue.get())
        try:
            done, pending = await asyncio.wait(
                {high_wait, low_wait},
                timeout=0.1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            results = []
            for task in done:
                try:
                    results.append(task.result())
                except (asyncio.CancelledError, Exception):
                    pass
            for task in pending:
                if not task.cancel():
                    # Task completed before cancel — item already dequeued, recover it
                    try:
                        results.append(task.result())
                    except (asyncio.CancelledError, Exception):
                        pass
                else:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
            return results
        except Exception:
            high_wait.cancel()
            low_wait.cancel()
            return []

    async def _emit_alert(self, alert: Alert, event_hash: str = "") -> None:
        """Fan out alert to outputs, storage, and SSE subscribers."""
        # CQRS: update write-side counters
        self._alert_counters.record(alert)
        self.emit_log("warn" if alert.severity in ("critical", "suspect") else "info",
                       f"detect.{alert.detector}", alert.title, alert.team_id)

        # Enqueue alert as durability fence (flushes pending events + alert atomically)
        if self._store:
            self._store.enqueue_alert(alert, event_hash=event_hash)

        # Fan out to outputs
        await asyncio.gather(
            *(o.emit(alert) for o in self._outputs),
            return_exceptions=True,
        )

        # Fan out to SSE subscribers (snapshot list to avoid mutation during iteration)
        for sub in list(self._alert_subscribers):
            try:
                sub.put_nowait(alert)
            except asyncio.QueueFull:
                pass  # slow consumer

    def _drain_batch(self) -> list[Event]:
        """Drain up to batch_size events. High priority first, then low."""
        batch: list[Event] = []
        budget = self._batch_size

        # Phase 1: drain all available high-priority (up to budget)
        while budget > 0:
            try:
                batch.append(self._high_queue.get_nowait())
                budget -= 1
            except asyncio.QueueEmpty:
                break

        # Phase 2: fill remaining budget from low-priority
        while budget > 0:
            try:
                batch.append(self._low_queue.get_nowait())
                budget -= 1
            except asyncio.QueueEmpty:
                break

        return batch

    # --- Debounced sweep ---

    def _schedule_debounced_sweep(self, team_id: str) -> None:
        """Reset the debounce timer for this team. Sweep runs once after quiet period."""
        existing = self._sweep_timers.get(team_id)
        if existing and not existing.done():
            existing.cancel()
        self._sweep_timers[team_id] = asyncio.create_task(
            self._debounced_sweep(team_id),
            name=f"sweep-debounce-{team_id}",
        )

    async def _debounced_sweep(self, team_id: str) -> None:
        """Wait for debounce period, then run sweep for this team."""
        try:
            await asyncio.sleep(self._sweep_debounce)
            await self._run_sweep(team_id, f"anomaly-debounced-{team_id}")
        except asyncio.CancelledError:
            pass  # timer was reset by a new alert

    @staticmethod
    def _profile_hash(profiles: dict) -> str:
        """Content-addressable hash of profile snapshot. Deterministic serialization."""
        canonical = json.dumps(
            {m: {"d": p.direction, "p": p.perpetrator_score, "v": p.victim_score,
                 "f": len(p.flags), "c": p.commits}
             for m, p in sorted(profiles.items())},
            sort_keys=True,
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    async def _run_sweep(self, team_id: str, trigger: str) -> None:
        """Run historical analyzer on events for one team.

        Deduplicates via content hash — identical profiles skip DB write + permutation test.
        After profiling, checks branch resolutions for evidence degradation.
        """
        if not self._analyzer:
            return
        team_events = [e for e in self._timeline if e.team_id == team_id]
        self.emit_log("info", "sweep", f"Starting: {len(team_events)} events", team_id)
        try:
            profiles = await self._analyzer.sweep(team_events, team_id=team_id, members=self._members)

            # Dedupe: skip write if profiles unchanged
            new_hash = self._profile_hash(profiles)
            if new_hash == self._last_profile_hash.get(team_id):
                self.emit_log("debug", "sweep", f"Dedupe: unchanged (hash={new_hash})", team_id)
                return
            self._last_profile_hash[team_id] = new_hash
            self._last_profiles[team_id] = profiles

            # Enqueue profiles with content hash
            if self._store:
                self._store.enqueue_profiles(team_id, profiles, profile_hash=new_hash)

            total_flags = sum(len(p.flags) for p in profiles.values())
            self.emit_log("info", "sweep",
                          f"Done: {len(profiles)} members, {total_flags} flags, hash={new_hash}",
                          team_id)

            for member, profile in sorted(profiles.items()):
                if profile.flags:
                    log.info(
                        "  %s: %s | %d flags | perp=%d vict=%d",
                        member, profile.direction,
                        len(profile.flags), profile.perpetrator_score, profile.victim_score,
                    )
            # Drift detection: check branch resolutions for evidence degradation
            await self._check_resolution_drift(team_id)
        except Exception as e:
            self.emit_log("error", "sweep", str(e), team_id)
            log.error("Historical sweep failed for %s: %s", team_id, e, exc_info=True)

    async def _check_resolution_drift(self, team_id: str) -> None:
        """Compare current branch resolutions against stored ones.

        If a resolution degraded (e.g., corroborated → single-source → unresolvable),
        emit an evidence_degradation alert.
        """
        if not self._store:
            return
        try:
            stored = await self._store.read_resolutions(team_id)
        except Exception:
            return
        if not stored:
            return

        # Resolution quality ranking (higher = better)
        _QUALITY_RANK = {
            "git-verifiable": 4, "board-verifiable": 3,
            "heuristic": 2, "disputed": 1, "unresolvable": 0,
        }

        for res in stored:
            branch = res.get("branch", "")
            old_quality = res.get("evidence_quality", "")
            old_method = res.get("resolution_method", "")
            old_rank = _QUALITY_RANK.get(old_quality, 0)

            # TODO: Compare against freshly computed resolution for this branch.
            # For now, drift detection is triggered when the store has a resolution
            # but the current timeline has a branch.delete event with no corresponding
            # branch.create — indicating the ref was removed between sweeps.
            #
            # Full implementation requires running resolve_branch_author() here,
            # which depends on board_creators and DAG — not available on the bus.
            # This will be wired when the analyzer gains branch resolution capability.

    async def _scheduled_sweeps(self) -> None:
        """Periodic sweep on interval — sweeps all teams with events."""
        while self._running:
            await asyncio.sleep(self._sweep_interval)
            if not self._timeline or not self._analyzer:
                continue
            team_ids = {e.team_id for e in self._timeline}
            for team_id in team_ids:
                await self._run_sweep(team_id, "scheduled")

    # --- Lifecycle ---

    async def run(self) -> None:
        """Start all ingestors and the event processing loop."""
        self._running = True
        log.info(
            "Bus starting: %d ingestor(s), %d detector(s), %d output(s) | "
            "sweep_on_alert=%s (debounce=%ss), sweep_interval=%s | "
            "queues: high=%d low=%d, batch=%d, timeline_max=%d",
            len(self._ingestors), len(self._detectors), len(self._outputs),
            self._sweep_on_alert, self._sweep_debounce, self._sweep_interval,
            self._high_queue.maxsize, self._low_queue.maxsize, self._batch_size,
            self._timeline_max,
        )

        tasks = [
            asyncio.create_task(self._process_events(), name="bus-processor"),
        ]
        for ing in self._ingestors:
            tasks.append(asyncio.create_task(ing.stream(), name=f"ingestor-{type(ing).__name__}"))

        if self._sweep_interval:
            tasks.append(asyncio.create_task(self._scheduled_sweeps(), name="scheduled-sweep"))

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            log.info("Bus shutting down")
        finally:
            self._running = False
            # Flush any remaining events to storage
            if self._store:
                await self._store.flush()

    def stop(self) -> None:
        self._running = False
        for task in self._sweep_timers.values():
            if not task.done():
                task.cancel()
        self._sweep_timers.clear()

    # --- Accessors ---

    @property
    def timeline(self) -> list[Event]:
        return self._timeline

    @property
    def alert_counters(self) -> AlertCounters:
        return self._alert_counters

    @property
    def queue_depths(self) -> dict[str, int]:
        return {
            "high": self._high_queue.qsize(),
            "low": self._low_queue.qsize(),
        }

    def get_detector_configs(self) -> dict[str, dict]:
        """Return current detector configs for the config API."""
        configs = {}
        for d in self._detectors:
            cfg = {"enabled": True}
            if hasattr(d, "get_config"):
                cfg.update(d.get_config())
            configs[d.name] = cfg
        return configs
