"""Quart app factory. REST API + webhook receiver + SSE streams.

All routes are generic — no hardcoded team names, URLs, or instance config.
The orchestrator is injected via create_app(). No globals.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path

from quart import Quart, jsonify, request, Response, send_from_directory

from .sse import alert_stream, metrics_stream, log_stream

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"


def create_app(orchestrator, github_webhook_secret: str = "") -> Quart:
    """Create the Quart app with all routes wired to the orchestrator.

    Args:
        orchestrator: Reconcile Orchestrator instance.
        github_webhook_secret: HMAC secret for GitHub webhook verification.
        Empty string = skip verification.
    """
    app = Quart(__name__, template_folder=str(TEMPLATE_DIR))
    app.config["GITHUB_WEBHOOK_SECRET"] = github_webhook_secret

    @app.after_request
    async def add_cors(response):
        # Skip CORS headers for SSE streams (chunked encoding incompatible)
        if response.content_type and "text/event-stream" in response.content_type:
            return response
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    @app.route("/api/<path:path>", methods=["OPTIONS"])
    async def cors_preflight(path):
        return "", 204

    # --- Dashboard ---

    @app.route("/")
    async def index():
        return await send_from_directory(str(TEMPLATE_DIR), "dashboard.html")

    # --- Static file serving (report, research docs, view.html) ---

    PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

    @app.route("/report")
    async def serve_report():
        return await send_from_directory(str(PROJECT_ROOT / "audit-output"), "comprehensive-report.html")

    @app.route("/view")
    async def serve_viewer():
        return await send_from_directory(str(PROJECT_ROOT), "view.html")

    @app.route("/research/<path:filename>")
    async def serve_research(filename):
        return await send_from_directory(str(PROJECT_ROOT / "research"), filename)

    @app.route("/audit-output/<path:filename>")
    async def serve_audit_output(filename):
        return await send_from_directory(str(PROJECT_ROOT / "audit-output"), filename)

    # --- Team CRUD ---

    @app.route("/api/teams", methods=["GET"])
    async def list_teams():
        # CQRS: read from write-side counters (zero DB cost)
        teams = []
        for tid, runner in orchestrator.teams.items():
            teams.append({
                "team_id": tid,
                "name": runner.config.team_name,
                "status": "running" if runner.bus._running else "stopped",
                "timeline_size": len(runner.bus.timeline),
                "detectors": len(runner.bus._detectors),
                "alert_count": runner.bus.alert_counters.total,
            })
        return jsonify(teams)

    @app.route("/api/teams", methods=["POST"])
    async def add_team():
        data = await request.get_json()
        if not data or "team_id" not in data:
            return jsonify({"error": "team_id required"}), 400
        from reconcile.orchestrator import TeamConfig
        config = TeamConfig(**data)
        orchestrator.add_team(config)
        return jsonify({"status": "added", "team_id": config.team_id}), 201

    @app.route("/api/teams/<team_id>", methods=["GET"])
    async def team_detail(team_id):
        runner = orchestrator.teams.get(team_id)
        if not runner:
            return jsonify({"error": "not found"}), 404
        profiles = []
        if orchestrator._store:
            profiles = await orchestrator._store.read_profiles(team_id)
        return jsonify({
            "team_id": team_id,
            "name": runner.config.team_name,
            "status": "running" if runner.bus._running else "stopped",
            "profiles": profiles,
        })

    @app.route("/api/teams/<team_id>", methods=["DELETE"])
    async def remove_team(team_id):
        if team_id not in orchestrator.teams:
            return jsonify({"error": "not found"}), 404
        orchestrator.remove_team(team_id)
        return jsonify({"status": "removed"})

    @app.route("/api/teams/<team_id>/sweep", methods=["POST"])
    async def trigger_sweep(team_id):
        if team_id not in orchestrator.teams:
            return jsonify({"error": "not found"}), 404
        if not orchestrator.sweep_team(team_id):
            return jsonify({"error": "sweep already in progress", "team_id": team_id}), 409
        return jsonify({"status": "started", "team_id": team_id})

    # --- Config ---

    @app.route("/api/teams/<team_id>/config", methods=["GET"])
    async def team_config(team_id):
        runner = orchestrator.teams.get(team_id)
        if not runner:
            return jsonify({"error": "not found"}), 404
        return jsonify({
            "sweep_on_alert": runner.bus._sweep_on_alert,
            "sweep_debounce": runner.bus._sweep_debounce,
            "sweep_interval": runner.bus._sweep_interval,
            "detectors": runner.bus.get_detector_configs(),
        })

    @app.route("/api/teams/<team_id>/config", methods=["PATCH"])
    async def update_team_config(team_id):
        runner = orchestrator.teams.get(team_id)
        if not runner:
            return jsonify({"error": "not found"}), 404
        data = await request.get_json()
        if not data:
            return jsonify({"error": "empty body"}), 400

        # Bus-level params
        if "sweep_on_alert" in data:
            runner.bus._sweep_on_alert = bool(data["sweep_on_alert"])
            runner.config.sweep_on_alert = runner.bus._sweep_on_alert
        if "sweep_debounce" in data:
            runner.bus._sweep_debounce = float(data["sweep_debounce"])
            runner.config.sweep_debounce = runner.bus._sweep_debounce
        if "sweep_interval" in data:
            runner.bus._sweep_interval = float(data["sweep_interval"]) if data["sweep_interval"] else None
            runner.config.sweep_interval = runner.bus._sweep_interval

        # Detector params
        if "detectors" in data:
            from datetime import timedelta
            from reconcile.detectors import discover_detectors
            import inspect

            available = discover_detectors()
            current_by_name = {d.name: d for d in runner.bus._detectors}

            for det_name, det_cfg in data["detectors"].items():
                if "enabled" in det_cfg and not det_cfg["enabled"]:
                    # Disable: remove from bus
                    if det_name in current_by_name:
                        runner.bus._detectors = [d for d in runner.bus._detectors if d.name != det_name]
                        current_by_name.pop(det_name)
                    continue

                if det_name not in current_by_name and det_name in available:
                    # Re-enable: instantiate with provided kwargs
                    cls = available[det_name]
                    init_params = inspect.signature(cls.__init__).parameters
                    kwargs = {k: v for k, v in det_cfg.items() if k != "enabled" and k in init_params}
                    detector = cls(**kwargs)
                    runner.bus.add_detector(detector)
                    current_by_name[det_name] = detector
                elif det_name in current_by_name:
                    # Update thresholds on live detector
                    detector = current_by_name[det_name]
                    for k, v in det_cfg.items():
                        if k == "enabled":
                            continue
                        # Map param names to detector attributes
                        if k == "window_seconds" and hasattr(detector, "window"):
                            detector.window = timedelta(seconds=int(v))
                        elif k == "activity_window_minutes" and hasattr(detector, "activity_window"):
                            detector.activity_window = timedelta(minutes=int(v))
                        elif k == "absence_comms_window_hours" and hasattr(detector, "absence_comms_window"):
                            detector.absence_comms_window = timedelta(hours=int(v))
                        elif k == "unexcused_absence_threshold" and hasattr(detector, "unexcused_threshold"):
                            detector.unexcused_threshold = int(v)
                        elif k == "frequent_absence_threshold" and hasattr(detector, "frequent_threshold"):
                            detector.frequent_threshold = int(v)
                        elif k == "min_cards" and hasattr(detector, "min_cards"):
                            detector.min_cards = int(v)

        return jsonify({"status": "updated"})

    @app.route("/api/teams/<team_id>/alerts", methods=["GET"])
    async def team_alerts(team_id):
        if not orchestrator._store or not orchestrator._store._db:
            return jsonify([])
        limit = request.args.get("limit", 100, type=int)
        severity = request.args.get("severity")
        alerts = await orchestrator._store.read_alerts(team_id, limit=limit, severity=severity)
        return jsonify(alerts)

    @app.route("/api/alerts/<int:alert_id>", methods=["GET"])
    async def alert_detail(alert_id):
        """Single alert with metadata + related event (if event_hash exists)."""
        if not orchestrator._store or not orchestrator._store._db:
            return jsonify({"error": "no store"}), 503
        cursor = await orchestrator._store._db.execute(
            "SELECT * FROM alerts WHERE id = ?", (alert_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        cols = [d[0] for d in cursor.description]
        alert = dict(zip(cols, row))
        # Attach triggering event if available
        if alert.get("event_hash"):
            cursor2 = await orchestrator._store._db.execute(
                "SELECT * FROM events WHERE event_hash = ? LIMIT 1", (alert["event_hash"],),
            )
            erow = await cursor2.fetchone()
            if erow:
                ecols = [d[0] for d in cursor2.description]
                alert["triggering_event"] = dict(zip(ecols, erow))
        # Attach related alerts (same team, same detector, ±1 hour)
        if alert.get("timestamp") and alert.get("team_id"):
            cursor3 = await orchestrator._store._db.execute(
                "SELECT id, timestamp, severity, title FROM alerts "
                "WHERE team_id = ? AND detector = ? AND id != ? "
                "ORDER BY ABS(JULIANDAY(timestamp) - JULIANDAY(?)) LIMIT 5",
                (alert["team_id"], alert.get("detector", ""), alert_id, alert["timestamp"]),
            )
            rows3 = await cursor3.fetchall()
            if rows3 and cursor3.description:
                rcols = [d[0] for d in cursor3.description]
                alert["related_alerts"] = [dict(zip(rcols, r)) for r in rows3]
        return jsonify(alert)

    @app.route("/api/teams/<team_id>/events", methods=["GET"])
    async def team_events(team_id):
        if not orchestrator._store:
            return jsonify([])
        limit = request.args.get("limit", 100, type=int)
        since = request.args.get("since")
        before = request.args.get("before")
        events = await orchestrator._store.read_events(
            team_id, since=since, before=before, limit=limit, newest_first=True,
        )
        return jsonify(events)

    # --- System Logs ---

    @app.route("/api/logs")
    async def get_logs():
        if not orchestrator._store or not orchestrator._store._db:
            return jsonify([])
        limit = request.args.get("limit", 200, type=int)
        team_id = request.args.get("team_id")
        level = request.args.get("level")
        logs = await orchestrator._store.read_logs(limit=limit, team_id=team_id, level=level)
        return jsonify(logs)

    # --- SSE Streams ---

    SSE_HEADERS = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Access-Control-Allow-Origin": "*",
        "Connection": "keep-alive",
    }

    @app.route("/api/alerts/stream")
    async def sse_alerts():
        async def generate():
            async for chunk in alert_stream(orchestrator):
                yield chunk
        return Response(generate(), content_type="text/event-stream", headers=SSE_HEADERS)

    @app.route("/api/metrics/stream")
    async def sse_metrics():
        async def generate():
            async for chunk in metrics_stream(orchestrator):
                yield chunk
        return Response(generate(), content_type="text/event-stream", headers=SSE_HEADERS)

    @app.route("/api/logs/stream")
    async def sse_logs():
        async def generate():
            async for chunk in log_stream(orchestrator):
                yield chunk
        return Response(generate(), content_type="text/event-stream", headers=SSE_HEADERS)

    # --- GitHub Webhook ---

    @app.route("/hooks/github", methods=["POST"])
    async def github_webhook():
        secret = app.config.get("GITHUB_WEBHOOK_SECRET", "")
        if secret:
            payload = await request.get_data()
            sig = request.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(
                secret.encode(), payload, hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(sig, expected):
                return jsonify({"error": "bad signature"}), 403

        data = await request.get_json()
        if not data:
            return jsonify({"error": "empty payload"}), 400

        event_type = request.headers.get("X-GitHub-Event", "")
        repo_name = data.get("repository", {}).get("name", "")
        team_id = orchestrator.repo_to_team.get(repo_name, "")

        if team_id and team_id in orchestrator.teams:
            # Normalize GitHub event into reconcile Event
            from reconcile.schema import Event, default_priority
            from datetime import datetime, timezone

            action_map = {
                "push": "commit.push",
                "create": "branch.create",
                "delete": "branch.delete",
            }
            action = action_map.get(event_type)
            if action:
                actor = data.get("sender", {}).get("login", "github")
                target = data.get("ref", "")
                event = Event(
                    timestamp=datetime.now(timezone.utc),
                    source="github",
                    team_id=team_id,
                    actor=actor,
                    action=action,
                    target=target,
                    target_type="branch" if "branch" in event_type else "commit",
                    metadata={"event_type": event_type, "repo": repo_name},
                    raw=data,
                    confidence="server-authoritative",
                    priority=default_priority(action),
                )
                runner = orchestrator.teams[team_id]
                await runner.bus.publish(event)

        return jsonify({"status": "ok"})

    # --- Inject (test/demo) ---

    @app.route("/api/teams/<team_id>/inject", methods=["POST"])
    async def inject_event(team_id):
        """Push a synthetic event into a team's bus. For testing when sources are down."""
        runner = orchestrator.teams.get(team_id)
        if not runner:
            return jsonify({"error": "not found"}), 404
        data = await request.get_json()
        if not data or "action" not in data:
            return jsonify({"error": "action required"}), 400

        from reconcile.schema import Event, default_priority
        from datetime import datetime, timezone

        # Accept timestamp from payload (for replay), fall back to now
        ts = datetime.now(timezone.utc)
        if data.get("timestamp"):
            try:
                ts = datetime.fromisoformat(data["timestamp"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        # Resolve pipeline IDs to names using team's pipeline_map
        metadata = dict(data.get("metadata", {}))
        pmap = runner.config.pipeline_map
        if pmap:
            for key in ("to_pipeline", "from_pipeline"):
                raw_val = str(metadata.get(key, ""))
                # Strip "pipeline:" prefix if present
                pid = raw_val.replace("pipeline:", "").strip()
                if pid in pmap:
                    metadata[f"{key}_name"] = pmap[pid]

        # Resolve member_id to canonical name (for card.assign events)
        mmap = runner.config.member_map
        if mmap and metadata.get("member_id"):
            mid = str(metadata["member_id"])
            if mid in mmap:
                metadata["assigned_member"] = mmap[mid]

        # Resolve actor through member_map (reverse) and git_author_map
        raw_actor = data.get("actor", "test-user")
        actor = raw_actor
        cfg = runner.config
        # git_author_map: git display name → canonical name
        if raw_actor in cfg.git_author_map:
            actor = cfg.git_author_map[raw_actor]
        # member_map: user_id → canonical name (reverse lookup by value for name match)
        elif raw_actor not in (cfg.member_map.values()):
            # Check if raw_actor is a user ID
            if raw_actor in cfg.member_map:
                actor = cfg.member_map[raw_actor]

        event = Event(
            timestamp=ts,
            source=data.get("source", "inject"),
            team_id=team_id,
            actor=actor,
            action=data["action"],
            target=data.get("target", ""),
            target_type=data.get("target_type", ""),
            metadata=metadata,
            raw=data,
            confidence="client-reported",
            priority=default_priority(data["action"]),
        )
        try:
            runner.bus.publish_nowait(event)
        except asyncio.QueueFull:
            return jsonify({"error": "queue full", "action": event.action}), 429
        return jsonify({"status": "injected", "action": event.action, "team_id": team_id})

    # --- Database management ---

    @app.route("/api/teams/<team_id>/clear", methods=["POST"])
    async def clear_team_data(team_id):
        """Wipe all events, alerts, and profiles for a team. Stops and restarts the team."""
        runner = orchestrator.teams.get(team_id)
        if not runner:
            return jsonify({"error": "not found"}), 404

        # Flush pending writes, then truncate team data from all tables.
        # Do NOT stop the bus — it needs to keep running for replay.
        store = orchestrator._store
        deleted_counts = {}
        if store and store._db:
            await store.flush()
            for table in ("events", "alerts", "profiles", "system_logs",
                          "metrics", "branch_resolutions"):
                try:
                    cur = await store._db.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE team_id = ?", (team_id,))
                    row = await cur.fetchone()
                    deleted_counts[table] = row[0] if row else 0
                    await store._db.execute(
                        f"DELETE FROM {table} WHERE team_id = ?", (team_id,))
                except Exception:
                    deleted_counts[table] = "skipped"
            await store._db.commit()
            await store._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        # Clear in-memory state (bus keeps running)
        runner.bus._timeline.clear()
        runner.bus._alert_counters._counts.clear()
        runner.bus._alert_counters.total = 0

        # Drain any stale events from queues
        while not runner.bus._high_queue.empty():
            try:
                runner.bus._high_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        while not runner.bus._low_queue.empty():
            try:
                runner.bus._low_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        return jsonify({
            "status": "cleared",
            "team_id": team_id,
            "deleted": deleted_counts,
        })

    @app.route("/api/teams/<team_id>/replay", methods=["POST"])
    async def replay_team(team_id):
        """Replay historical board + git events through the live pipeline.
        Uses scripts/replay.py loaders (full branch resolution, metadata enrichment).
        Body: {"speed": 10000, "limit": 0, "source": "all"}
        """
        import sys
        from reconcile.schema import Event, default_priority

        runner = orchestrator.teams.get(team_id)
        if not runner:
            return jsonify({"error": "not found"}), 404

        body = await request.get_json() or {}
        speed = body.get("speed", 10000)
        limit = body.get("limit", 0)
        source = body.get("source", "all")

        project_root = Path(__file__).resolve().parent.parent.parent

        # Import replay.py's proven loaders (full branch resolution via DAG)
        sys.path.insert(0, str(project_root))
        try:
            from scripts.replay import load_board_events, load_git_events, parse_timestamp
        except ImportError as e:
            return jsonify({"error": f"Cannot import scripts.replay: {e}"}), 500

        # ── Load events using replay.py's loaders ──
        events = []
        if source in ("board", "all"):
            board_path = project_root / "data" / "board-activity-full.json"
            if board_path.exists():
                board_events = load_board_events(str(board_path))
                events.extend(board_events)
                log.info("Replay: loaded %d board events", len(board_events))

        if source in ("git", "all"):
            git_path = project_root / "data" / "s26-fresh-clone"
            if git_path.exists():
                git_events = load_git_events(str(git_path))
                events.extend(git_events)
                log.info("Replay: loaded %d git events (with branch resolution)", len(git_events))

        if not events:
            return jsonify({"error": "no events found", "source": source}), 404

        # ── Parse timestamps and sort ──
        parsed = []
        for e in events:
            ts = parse_timestamp(e.get("timestamp", ""))
            if ts:
                e["_ts"] = ts
                parsed.append(e)
        parsed.sort(key=lambda e: e["_ts"])

        if limit:
            parsed = parsed[:limit]

        total = len(parsed)
        cfg = runner.config

        # ── Inject through the bus ──
        injected = 0
        prev_ts = parsed[0]["_ts"] if parsed else None

        for event_data in parsed:
            ts = event_data["_ts"]

            # Time compression delay
            if speed > 0 and prev_ts and ts > prev_ts:
                delay = (ts - prev_ts).total_seconds() / speed
                if delay > 0.001:
                    await asyncio.sleep(min(delay, 0.5))
            prev_ts = ts

            # Resolve actor
            raw_actor = event_data.get("actor", "replay")
            actor = raw_actor
            if raw_actor in cfg.git_author_map:
                actor = cfg.git_author_map[raw_actor]
            elif raw_actor in cfg.member_map:
                actor = cfg.member_map[raw_actor]

            # Resolve pipeline IDs to names
            metadata = dict(event_data.get("metadata", {}))
            pmap = cfg.pipeline_map
            if pmap:
                for key in ("to_pipeline", "from_pipeline"):
                    raw_val = str(metadata.get(key, ""))
                    pid = raw_val.replace("pipeline:", "").strip()
                    if pid in pmap:
                        metadata[f"{key}_name"] = pmap[pid]

            # Resolve member IDs to names
            mmap = cfg.member_map
            if mmap and metadata.get("member_id"):
                mid = str(metadata["member_id"])
                if mid in mmap:
                    metadata["assigned_member"] = mmap[mid]

            event = Event(
                timestamp=ts,
                source=event_data.get("source", "replay"),
                team_id=team_id,
                actor=actor,
                action=event_data["action"],
                target=event_data.get("target", ""),
                target_type=event_data.get("target_type", ""),
                metadata=metadata,
                raw=event_data,
                confidence="replay",
                priority=default_priority(event_data["action"]),
            )
            try:
                runner.bus.publish_nowait(event)
                injected += 1
            except asyncio.QueueFull:
                await asyncio.sleep(0.01)
                try:
                    runner.bus.publish_nowait(event)
                    injected += 1
                except asyncio.QueueFull:
                    break

        return jsonify({
            "status": "replay_complete",
            "team_id": team_id,
            "total_events": total,
            "injected": injected,
            "speed": speed,
            "source": source,
        })

    @app.route("/api/database/status")
    async def database_status():
        """Read-only DB inspection: row counts per table and team."""
        store = orchestrator._store
        if not store or not store._db:
            return jsonify({"error": "no store"}), 503

        result = {}
        for table in ("events", "alerts", "profiles", "logs"):
            try:
                cur = await store._db.execute(f"SELECT COUNT(*) FROM {table}")
                row = await cur.fetchone()
                result[table] = row[0] if row else 0
            except Exception:
                result[table] = "error"

        # Per-team breakdown
        teams = {}
        try:
            cur = await store._db.execute("SELECT DISTINCT team_id FROM events")
            rows = await cur.fetchall()
            for (tid,) in rows:
                cur2 = await store._db.execute("SELECT COUNT(*) FROM events WHERE team_id = ?", (tid,))
                row2 = await cur2.fetchone()
                cur3 = await store._db.execute("SELECT COUNT(*) FROM alerts WHERE team_id = ?", (tid,))
                row3 = await cur3.fetchone()
                teams[tid] = {"events": row2[0], "alerts": row3[0]}
        except Exception:
            pass

        result["teams"] = teams
        return jsonify(result)

    # --- Report ---

    @app.route("/api/teams/<team_id>/report")
    async def team_report(team_id):
        """Generate markdown report from live engine data."""
        runner = orchestrator.teams.get(team_id)
        if not runner:
            return jsonify({"error": "not found"}), 404

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        name = runner.config.team_name or team_id

        # Gather data
        profiles = []
        alerts = []
        if orchestrator._store:
            profiles = await orchestrator._store.read_profiles(team_id)
            alerts = await orchestrator._store.read_alerts(team_id, limit=1000)

        counters = runner.bus.alert_counters.snapshot()
        timeline_size = len(runner.bus.timeline)

        # Build markdown
        lines = [
            f"# Team Attribution Report — {name}",
            f"Generated: {now}\n",
            f"## Data Summary",
            f"- Events in timeline: {timeline_size}",
            f"- Total alerts: {counters['total']}",
            f"- Detectors active: {len(runner.bus._detectors)}\n",
        ]

        # Profiles table
        if profiles:
            lines.append("## Member Profiles\n")
            lines.append("| Member | Direction | P Score | V Score | Commits | Cards | Zero-Commit | Branches Del | Meetings Present | Meetings Absent |")
            lines.append("|--------|-----------|:-------:|:-------:|:-------:|:-----:|:-----------:|:------------:|:----------------:|:---------------:|")
            for p in profiles:
                flags = p.get("flags", "[]")
                if isinstance(flags, str):
                    import json as _json
                    try:
                        flags = _json.loads(flags)
                    except Exception:
                        flags = []
                lines.append(
                    f"| {p.get('member', '?')} | {p.get('direction', '?')} | "
                    f"{p.get('perpetrator_score', 0)} | {p.get('victim_score', 0)} | "
                    f"{p.get('commits', 0)} | {p.get('cards_completed', 0)} | "
                    f"{p.get('cards_completed_zero_commits', 0)} | {p.get('branches_deleted', 0)} | "
                    f"{p.get('meetings_present', 0)} | {p.get('meetings_absent', 0)} |"
                )

            # Flags detail
            lines.append("\n## Flags Detail\n")
            for p in profiles:
                flags = p.get("flags", "[]")
                if isinstance(flags, str):
                    import json as _json
                    try:
                        flags = _json.loads(flags)
                    except Exception:
                        flags = []
                if flags:
                    lines.append(f"### {p.get('member', '?')} ({p.get('direction', '?')})\n")
                    for f in flags[:20]:
                        date = f.get("date", "?")
                        if isinstance(date, str) and len(date) > 10:
                            date = date[:10]
                        lines.append(f"- **{f.get('type', '?')}** ({f.get('severity', '?')}) {date}: {f.get('detail', '')}")
                    lines.append("")

        # Alert summary
        if counters["total"] > 0:
            lines.append("## Alert Summary\n")
            lines.append("| Severity | Count |")
            lines.append("|----------|:-----:|")
            for sev in ("critical", "suspect", "elevated", "info"):
                c = counters["by_severity"].get(sev, 0)
                if c > 0:
                    lines.append(f"| {sev} | {c} |")
            lines.append("")
            lines.append("| Category | Count |")
            lines.append("|----------|:-----:|")
            for cat in ("evidence", "attribution", "attendance", "process"):
                c = counters["by_category"].get(cat, 0)
                if c > 0:
                    lines.append(f"| {cat} | {c} |")
            lines.append("")

        # Top alerts
        if alerts:
            lines.append("## Top Alerts (by score)\n")
            sorted_alerts = sorted(alerts, key=lambda a: -(a.get("score", 0)))[:20]
            for a in sorted_alerts:
                lines.append(f"- **[{a.get('severity', '?').upper()}]** {a.get('category', '?')} | {a.get('title', '?')} | {a.get('detail', '')[:100]}")
            lines.append("")

        # Methodology
        lines.append("## Methodology\n")
        lines.append("- **Detection:** 8 auto-discovered detectors (zero-commit-complete, branch-delete-before-complete, batch-completion, file-reattribution, completion-non-assignee, unrecorded-deletion, report-revision, attendance-anomaly)")
        lines.append("- **Scoring:** Composite = category_weight (1-4) x severity_weight (1-4), range 1-16")
        lines.append("- **Categories:** process (1), attendance (2), attribution (3), evidence (4)")
        lines.append("- **Historical sweep:** Two-pass analysis on full event timeline")
        lines.append(f"- **Events analyzed:** {timeline_size}")
        lines.append("")

        report = "\n".join(lines)
        return Response(report, content_type="text/markdown", headers={
            "Content-Disposition": f"attachment; filename=report-{team_id}.md",
        })

    # --- Health ---

    @app.route("/api/health")
    async def health():
        return jsonify({
            "status": "ok",
            "teams": len(orchestrator.teams),
        })

    return app
