#!/usr/bin/env python3
"""
reconcile — real-time multi-team attribution integrity monitor

Usage:
  python -m reconcile.main --live     # WebSocket + polling, all configured teams
  python -m reconcile.main --batch    # One-shot analysis, all teams in parallel
  python -m reconcile.main --sweep    # Historical sweep only, then exit

Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from reconcile.orchestrator import Orchestrator, TeamConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-30s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("reconcile")


def load_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def load_teams() -> list[TeamConfig]:
    """Load team configs. Override by creating reconcile/config_local.py."""
    try:
        from reconcile.config_local import TEAMS
        log.info("Loaded %d team(s) from config_local.py", len(TEAMS))
        return TEAMS
    except ImportError:
        pass

    # Default: single team from config_template
    try:
        import reconcile.config_template as cfg
    except ImportError:
        cfg = None

    team = TeamConfig(
        team_id=os.environ.get("TEAM_ID", getattr(cfg, "TEAM_ID", "default")),
        team_name=os.environ.get("TEAM_NAME", getattr(cfg, "TEAM_NAME", "")),
        ws_url=os.environ.get("WS_URL", getattr(cfg, "WS_URL", "")),
        git_repo=os.environ.get("GIT_REPO", getattr(cfg, "GIT_REPO", "")),
        discord_token=os.environ.get("DISCORD_TOKEN", ""),
        discord_channels=getattr(cfg, "DISCORD_CHANNELS", []),
        email_dir=getattr(cfg, "EMAIL_DIR", "statusreports/"),
        member_map=getattr(cfg, "MEMBER_MAP", {}),
        git_author_map=getattr(cfg, "GIT_AUTHOR_MAP", {}),
        pm_user_id=getattr(cfg, "PM_USER_ID", ""),
        sweep_on_alert=getattr(cfg, "SWEEP_ON_ALERT", True),
        sweep_interval=getattr(cfg, "SWEEP_INTERVAL", 86400),
        git_poll_interval=getattr(cfg, "GIT_POLL_INTERVAL", 60),
        discord_poll_interval=getattr(cfg, "DISCORD_POLL_INTERVAL", 120),
        detectors=getattr(cfg, "DETECTORS", {}),
        output_dir="audit-output",
    )
    return [team]


async def run(mode: str):
    load_env()
    teams = load_teams()

    orch = Orchestrator(mode=mode)
    for team in teams:
        orch.add_team(team)

    if mode == "sweep":
        # One-shot: ingest existing data, sweep, print profiles, exit
        await orch._store.init()
        for runner in orch.teams.values():
            for ing in runner.bus._ingestors:
                try:
                    await asyncio.wait_for(ing.stream(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        # One-shot sweep: run directly (not as background task)
        for team_id, runner in orch.teams.items():
            await runner.bus._run_sweep(team_id, f"cli-sweep-{team_id}")
            profiles = runner.bus._last_profiles.get(team_id, {})
            log.info("=== Team %s ===", team_id)
            for member, profile in sorted(profiles.items()):
                log.info(
                    "  %s: %s | %d flags | commits=%d msgs=%d | perp=%d vict=%d",
                    member, profile.direction, len(profile.flags),
                    profile.commits, profile.messages_sent,
                    profile.perpetrator_score, profile.victim_score,
                )
        await orch._store.close()
        return

    # Live/batch: register signal handlers for graceful shutdown
    shutdown_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    orch_task = asyncio.create_task(orch.run())
    await shutdown_event.wait()

    for runner in orch.teams.values():
        runner.bus.stop()
    orch_task.cancel()
    try:
        await asyncio.gather(orch_task, return_exceptions=True)
    except Exception:
        pass
    await orch._store.close()
    log.info("Stopped")


async def serve(host: str, port: int):
    """Start dashboard + orchestrator on the same event loop."""
    load_env()
    teams = load_teams()

    orch = Orchestrator(mode="live")
    for team in teams:
        orch.add_team(team)

    from reconcile.web.app import create_app
    app = create_app(orch, github_webhook_secret=os.environ.get("GITHUB_WEBHOOK_SECRET", ""))

    # Store init + writer start handled by orch.run()
    log.info("Dashboard at http://%s:%d", host, port)
    log.info("Press Ctrl+C to stop")

    shutdown_event = asyncio.Event()

    def _signal_handler():
        log.info("Shutting down...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    # Use Hypercorn directly to disable response timeout (SSE streams are long-lived)
    from hypercorn.asyncio import serve as hypercorn_serve
    from hypercorn.config import Config as HypercornConfig
    hconfig = HypercornConfig()
    hconfig.bind = [f"{host}:{port}"]
    hconfig.response_timeout = None  # SSE streams live indefinitely
    hconfig.keep_alive_timeout = 600  # 10 min keep-alive for idle connections

    server_task = asyncio.create_task(hypercorn_serve(app, hconfig, shutdown_trigger=shutdown_event.wait))
    orch_task = asyncio.create_task(orch.run())

    await shutdown_event.wait()

    # Graceful shutdown
    for runner in orch.teams.values():
        runner.bus.stop()
    orch_task.cancel()
    server_task.cancel()
    try:
        await asyncio.gather(server_task, orch_task, return_exceptions=True)
    except Exception:
        pass
    await orch._store.close()
    log.info("Stopped")


def main():
    parser = argparse.ArgumentParser(
        description="reconcile — multi-team attribution integrity monitor"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--serve", action="store_true", help="Dashboard + live monitoring")
    group.add_argument("--live", action="store_true", help="Real-time: WebSocket + polling")
    group.add_argument("--batch", action="store_true", help="Batch: one-shot analysis")
    group.add_argument("--sweep", action="store_true", help="Historical sweep only, then exit")
    parser.add_argument("--host", default="0.0.0.0", help="Dashboard bind address")
    parser.add_argument("--port", type=int, default=8080, help="Dashboard port")
    args = parser.parse_args()

    try:
        if args.serve:
            asyncio.run(serve(args.host, args.port))
        else:
            if args.live:
                mode = "live"
            elif args.sweep:
                mode = "sweep"
            else:
                mode = "batch"
            asyncio.run(run(mode))
    except KeyboardInterrupt:
        log.info("Stopped")


if __name__ == "__main__":
    main()
