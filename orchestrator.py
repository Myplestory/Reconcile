"""Async orchestrator — runs Reconcile across multiple teams in parallel.

Three modes:
  1. Live:     WebSocket + polling per team, real-time detection + periodic sweep
  2. Batch:    One-shot ingest → analyze → output per team, all teams in parallel
  3. Sweep:    Ingest existing data, run historical analysis, print profiles, exit

Each team gets its own EventBus with its own detectors, analyzer, and timeline.
Teams run as independent async tasks — no shared mutable state between teams.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from .schema import Event, Alert
from .bus import EventBus
from .analyzer import HistoricalAnalyzer
from .storage import Store
from .detectors import discover_detectors
from .ingestors.ws_board import BoardWSIngestor
from .ingestors.git_poll import GitPollIngestor
from .outputs.console import ConsoleOutput
from .outputs.json_file import JSONFileOutput

log = logging.getLogger(__name__)


@dataclass
class TeamConfig:
    """Configuration for one team. Swap these to point at different projects."""

    team_id: str
    team_name: str = ""

    # Sources
    ws_url: str = ""
    git_repo: str = ""
    discord_token: str = ""
    discord_channels: list[str] = field(default_factory=list)
    email_dir: str = ""

    # Identity
    member_map: dict[str, str] = field(default_factory=dict)
    git_author_map: dict[str, str] = field(default_factory=dict)
    pm_user_id: str = ""

    # Demux (for shared ingestors)
    board_id: str = ""            # board tool board → this team
    ws_source_name: str = "board-ws"  # Event.source for board WS events

    # Board column/pipeline mapping (ID → name). Enables detectors to
    # recognize completion columns regardless of board tool's internal IDs.
    pipeline_map: dict[str, str] = field(default_factory=dict)
    github_repos: list[str] = field(default_factory=list)  # repo names → this team
    discord_guild_id: str = ""    # Discord guild → this team

    # Behavior
    sweep_on_alert: bool = True
    sweep_interval: float | None = 86400  # daily
    sweep_debounce: float = 30.0
    git_poll_interval: float = 60.0
    discord_poll_interval: float = 120.0

    # Detector thresholds
    detectors: dict[str, dict] = field(default_factory=lambda: {
        "zero-commit-complete": {"enabled": True},
        "branch-delete-before-complete": {"enabled": True, "window_seconds": 300},
        "batch-completion": {"enabled": True, "window_seconds": 60, "min_cards": 3},
        "file-reattribution": {"enabled": True},
    })

    # Outputs
    output_dir: str = "audit-output"
    console_output: bool = True
    json_output: bool = True


class TeamRunner:
    """Manages one team's event bus, ingestors, detectors, and analyzer."""

    def __init__(self, config: TeamConfig, mode: str = "live", store: Store | None = None):
        self.config = config
        self.mode = mode
        self.store = store
        self.bus = EventBus(
            sweep_on_alert=config.sweep_on_alert,
            sweep_interval=config.sweep_interval if mode == "live" else None,
            sweep_debounce=config.sweep_debounce,
        )
        self._analyzer = HistoricalAnalyzer()
        self.bus.set_analyzer(self._analyzer)
        if store:
            self.bus.set_store(store)
        # Set known members for profile filtering
        if config.member_map:
            self.bus._members = set(config.member_map.values())
        self._wire()

    def _wire(self):
        """Connect detectors, outputs, and ingestors based on config."""
        cfg = self.config
        det = cfg.detectors

        # Detectors (auto-discovered)
        available = discover_detectors()
        for name, cls in available.items():
            dcfg = det.get(name, {})
            if not dcfg.get("enabled", True):
                continue
            # Pass recognized kwargs from config to constructor
            init_params = inspect.signature(cls.__init__).parameters
            kwargs = {k: v for k, v in dcfg.items() if k != "enabled" and k in init_params}
            self.bus.add_detector(cls(**kwargs))

        # Outputs
        if cfg.console_output:
            self.bus.add_output(ConsoleOutput())
        if cfg.json_output:
            os.makedirs(cfg.output_dir, exist_ok=True)
            path = os.path.join(cfg.output_dir, f"live-alerts-{cfg.team_id}.jsonl")
            self.bus.add_output(JSONFileOutput(path))

        # Ingestors
        if self.mode == "live" and cfg.ws_url:
            self.bus.add_ingestor(BoardWSIngestor(
                cfg.ws_url,
                source_name=cfg.ws_source_name,
                member_map=cfg.member_map,
                pipeline_map=cfg.pipeline_map,
                default_team_id=cfg.team_id,
            ))

        git_interval = cfg.git_poll_interval if self.mode == "live" else 999999
        if cfg.git_repo and os.path.isdir(os.path.join(cfg.git_repo, ".git")):
            self.bus.add_ingestor(GitPollIngestor(
                cfg.git_repo,
                team_id=cfg.team_id,
                member_map=cfg.git_author_map,
                interval=git_interval,
            ))

    async def run(self):
        """Run this team's bus. Blocks until stopped."""
        log.info("Team %s (%s) starting in %s mode", self.config.team_id, self.config.team_name, self.mode)
        await self.bus.run()


class Orchestrator:
    """Multi-team async orchestrator.

    Usage:
        orch = Orchestrator(mode="live")
        orch.add_team(TeamConfig(team_id="team-a", ...))
        orch.add_team(TeamConfig(team_id="team-b", ...))
        await orch.run()  # both teams run in parallel
    """

    def __init__(self, mode: str = "live", db_path: str = "data/reconcile.db"):
        self.mode = mode
        self._teams: dict[str, TeamRunner] = {}
        self._tasks: dict[str, asyncio.Task] = {}

        # Shared storage (one DB for all teams)
        self._store = Store(db_path=db_path)

        # Sweep dedup guard — prevents concurrent sweeps on the same team
        self._sweeps_in_progress: set[str] = set()

        # Reverse mappings for shared ingestors (GitHub webhooks, etc.)
        self.repo_to_team: dict[str, str] = {}    # repo_name -> team_id
        self.board_to_team: dict[str, str] = {}    # board_id -> team_id
        self.guild_to_team: dict[str, str] = {}    # guild_id -> team_id

        # SSE: central alert + log subscribers (bridges team buses → dashboard)
        self._alert_subscribers: set[asyncio.Queue] = set()
        self._log_subscribers: set[asyncio.Queue] = set()

    def add_team(self, config: TeamConfig) -> None:
        runner = TeamRunner(config, mode=self.mode, store=self._store)
        self._teams[config.team_id] = runner

        # Build reverse mappings for demux
        for repo in config.github_repos:
            self.repo_to_team[repo] = config.team_id
        if config.board_id:
            self.board_to_team[config.board_id] = config.team_id
        if config.discord_guild_id:
            self.guild_to_team[config.discord_guild_id] = config.team_id

        # Wire SSE subscribers into this team's bus
        for sub in self._alert_subscribers:
            runner.bus.subscribe_alerts(sub)
        for sub in self._log_subscribers:
            runner.bus.subscribe_logs(sub)

        log.info("Registered team: %s (%s)", config.team_id, config.team_name)

    def remove_team(self, team_id: str) -> None:
        runner = self._teams.pop(team_id, None)
        if runner:
            runner.bus.stop()
            # Evict detector state to free memory
            for detector in runner.bus._detectors:
                if hasattr(detector, "evict_team"):
                    detector.evict_team(team_id)
            # Clean up reverse mappings
            self.repo_to_team = {k: v for k, v in self.repo_to_team.items() if v != team_id}
            self.board_to_team = {k: v for k, v in self.board_to_team.items() if v != team_id}
            self.guild_to_team = {k: v for k, v in self.guild_to_team.items() if v != team_id}
            # Cancel task if running
            task = self._tasks.pop(team_id, None)
            if task and not task.done():
                task.cancel()
            log.info("Removed team: %s", team_id)

    # --- SSE subscriber management ---

    def subscribe_alerts(self, queue: asyncio.Queue) -> None:
        """Subscribe to alerts from ALL teams. Used by SSE endpoint."""
        self._alert_subscribers.add(queue)
        for runner in self._teams.values():
            runner.bus.subscribe_alerts(queue)

    def unsubscribe_alerts(self, queue: asyncio.Queue) -> None:
        self._alert_subscribers.discard(queue)
        for runner in self._teams.values():
            runner.bus.unsubscribe_alerts(queue)

    def subscribe_logs(self, queue: asyncio.Queue) -> None:
        """Subscribe to logs from ALL teams. Used by SSE endpoint."""
        self._log_subscribers.add(queue)
        for runner in self._teams.values():
            runner.bus.subscribe_logs(queue)

    def unsubscribe_logs(self, queue: asyncio.Queue) -> None:
        self._log_subscribers.discard(queue)
        for runner in self._teams.values():
            runner.bus.unsubscribe_logs(queue)

    # --- Run ---

    async def run(self) -> None:
        """Run all teams in parallel. Each team is an independent async task."""
        if not self._teams:
            log.error("No teams registered")
            return

        # Initialize shared storage + start writer
        await self._store.init()
        await self._store.start_writer()

        # Hydrate CQRS counters from DB (crash recovery)
        for team_id, runner in self._teams.items():
            rows = await self._store.hydrate_alert_counters(team_id)
            if rows:
                runner.bus.alert_counters.hydrate(rows)
                log.debug("Hydrated %s: %d counter rows", team_id, len(rows))

        log.info("=" * 60)
        log.info("Reconcile Orchestrator")
        log.info("Mode: %s | Teams: %d | DB: %s", self.mode, len(self._teams), self._store.db_path)
        log.info("=" * 60)

        for team_id, runner in self._teams.items():
            task = asyncio.create_task(
                runner.run(),
                name=f"team-{team_id}",
            )
            self._tasks[team_id] = task

        # Engine-level watchdog
        watchdog_task = asyncio.create_task(
            self._watchdog(), name="engine-watchdog",
        )

        try:
            await asyncio.gather(*self._tasks.values(), watchdog_task)
        except asyncio.CancelledError:
            log.info("Orchestrator shutting down")
        except KeyboardInterrupt:
            log.info("Stopped by user")
            for runner in self._teams.values():
                runner.bus.stop()
        finally:
            watchdog_task.cancel()
            await self._store.close()

    async def _watchdog(self, interval: float = 10.0) -> None:
        """Engine-level health monitor. Checks all critical components periodically.

        Detects: dead team tasks, dead writer, stalled bus (queue filling, timeline frozen),
        ingestor disconnects. Emits structured logs for dashboard visibility.
        """
        last_timeline_sizes: dict[str, int] = {}
        stall_counts: dict[str, int] = {}

        while True:
            await asyncio.sleep(interval)

            # 1. Check store writer liveness
            if self._store._writer_task and self._store._writer_task.done():
                exc = self._store._writer_task.exception() if not self._store._writer_task.cancelled() else None
                log.critical("WATCHDOG: Store writer is DEAD%s", f" — {exc}" if exc else "")
                # Emit to all buses so dashboard sees it
                for runner in self._teams.values():
                    runner.bus.emit_log("critical", "watchdog", "Store writer died — persistence halted")

            # 2. Check team task liveness
            for team_id, task in list(self._tasks.items()):
                if task.done() and not task.cancelled():
                    exc = task.exception() if not task.cancelled() else None
                    log.error("WATCHDOG: Team %s task died%s", team_id, f" — {exc}" if exc else "")
                    runner = self._teams.get(team_id)
                    if runner:
                        runner.bus.emit_log("critical", "watchdog", f"Team task died: {exc or 'exited'}")

            # 3. Check for stalled buses (queues filling, timeline not growing)
            for team_id, runner in self._teams.items():
                bus = runner.bus
                depths = bus.queue_depths
                total_depth = depths["high"] + depths["low"]
                timeline_size = len(bus.timeline)

                # Queue pressure: warn if > 80% capacity
                high_cap = bus._high_queue.maxsize
                low_cap = bus._low_queue.maxsize
                if depths["high"] > high_cap * 0.8:
                    log.warning("WATCHDOG: %s high queue at %d%% (%d/%d)",
                                team_id, int(depths["high"] / high_cap * 100), depths["high"], high_cap)
                    bus.emit_log("warning", "watchdog",
                                 f"High queue pressure: {depths['high']}/{high_cap}")
                if depths["low"] > low_cap * 0.8:
                    log.warning("WATCHDOG: %s low queue at %d%% (%d/%d)",
                                team_id, int(depths["low"] / low_cap * 100), depths["low"], low_cap)

                # Stall detection: queues have items but timeline hasn't grown
                prev_size = last_timeline_sizes.get(team_id, 0)
                if total_depth > 100 and timeline_size == prev_size:
                    stall_counts[team_id] = stall_counts.get(team_id, 0) + 1
                    if stall_counts[team_id] >= 3:  # 3 consecutive checks = 30s stall
                        log.error("WATCHDOG: %s bus appears stalled — %d queued, timeline frozen at %d",
                                  team_id, total_depth, timeline_size)
                        bus.emit_log("critical", "watchdog",
                                     f"Bus stalled: {total_depth} queued, timeline frozen at {timeline_size}")
                else:
                    stall_counts[team_id] = 0
                last_timeline_sizes[team_id] = timeline_size

            # 4. Check write channel pressure
            if self._store._channel:
                ch_size = self._store._channel.qsize()
                ch_max = self._store._channel.maxsize
                if ch_size > ch_max * 0.8:
                    log.warning("WATCHDOG: Write channel at %d%% (%d/%d)",
                                int(ch_size / ch_max * 100), ch_size, ch_max)
                    for runner in self._teams.values():
                        runner.bus.emit_log("warning", "watchdog",
                                            f"Write channel pressure: {ch_size}/{ch_max}")

    def sweep_all(self) -> None:
        """Fire historical sweep on all teams as background tasks."""
        for team_id in self._teams:
            self.sweep_team(team_id)

    def sweep_team(self, team_id: str) -> bool:
        """Fire sweep as background task. Returns False if already in progress."""
        runner = self._teams.get(team_id)
        if not runner:
            return False
        if team_id in self._sweeps_in_progress:
            return False
        self._sweeps_in_progress.add(team_id)
        task = asyncio.create_task(
            runner.bus._run_sweep(team_id, f"on-demand-{team_id}"),
            name=f"sweep-{team_id}",
        )
        task.add_done_callback(lambda _: self._sweeps_in_progress.discard(team_id))
        return True

    @property
    def teams(self) -> dict[str, TeamRunner]:
        return self._teams
