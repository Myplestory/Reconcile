# API Reference

All endpoints are served by the Quart app created in [`reconcile/web/app.py`](../reconcile/web/app.py).

Base URL: `http://localhost:{port}` (default 8080).

---

## Teams

### `GET /api/teams`

List all registered teams.

**Response** `200`
```json
[
  {
    "team_id": "team-a",
    "name": "Alpha",
    "status": "running",
    "timeline_size": 1234,
    "detectors": 8,
    "alert_count": 47
  }
]
```

| Field | Type | Description |
|-------|------|-------------|
| `team_id` | string | Unique team identifier |
| `name` | string | Display name (from [`TeamConfig.team_name`](../reconcile/orchestrator.py)) |
| `status` | string | `"running"` or `"stopped"` |
| `timeline_size` | int | Events in the in-memory timeline |
| `detectors` | int | Number of active detectors |
| `alert_count` | int | Total alerts in DB for this team |

---

### `POST /api/teams`

Register a new team.

**Request Body**
```json
{
  "team_id": "team-b",
  "team_name": "Beta",
  "sweep_on_alert": true
}
```

All fields from [`TeamConfig`](../reconcile/orchestrator.py) are accepted.

**Response** `201`
```json
{"status": "added", "team_id": "team-b"}
```

**Errors**: `400` if `team_id` missing.

---

### `GET /api/teams/:team_id`

Team detail with member profiles.

**Response** `200`
```json
{
  "team_id": "team-a",
  "name": "Alpha",
  "status": "running",
  "profiles": [
    {
      "member": "alice",
      "direction": "perpetrator",
      "perpetrator_score": 6,
      "victim_score": 0,
      "flags": "[{\"type\": \"zero-commit-completion\", ...}]",
      "commits": 12,
      "messages_sent": 8,
      "version": 3
    }
  ]
}
```

Profiles are from the latest version in the [`profiles`](../reconcile/storage.py) table.

**Errors**: `404` if team not found.

---

### `DELETE /api/teams/:team_id`

Remove a team. Stops its bus, evicts detector state, cleans reverse mappings.

**Response** `200`
```json
{"status": "removed"}
```

**Errors**: `404` if team not found.

---

## Configuration

### `GET /api/teams/:team_id/config`

Read current detector thresholds and sweep settings.

**Response** `200`
```json
{
  "sweep_on_alert": true,
  "sweep_debounce": 30.0,
  "sweep_interval": 86400,
  "detectors": {
    "zero-commit-complete": {"enabled": true},
    "batch-completion": {"enabled": true, "window_seconds": 60, "min_cards": 3},
    "attendance-anomaly": {
      "enabled": true,
      "activity_window_minutes": 120,
      "absence_comms_window_hours": 24,
      "unexcused_absence_threshold": 2,
      "frequent_absence_threshold": 3
    }
  }
}
```

Config is read live from the running [`EventBus`](../reconcile/bus.py) via [`get_detector_configs()`](../reconcile/bus.py).

---

### `PATCH /api/teams/:team_id/config`

Update config on a running team. No restart required.

**Request Body** (partial — only include fields to change)
```json
{
  "sweep_debounce": 60.0,
  "detectors": {
    "batch-completion": {"min_cards": 5},
    "attendance-anomaly": {"enabled": false}
  }
}
```

Changes apply immediately to the running detectors. Detector thresholds are mutated on live instances. Disabling a detector removes it from the bus; re-enabling re-instantiates it from [`discover_detectors()`](../reconcile/detectors/__init__.py).

**Response** `200`
```json
{"status": "updated"}
```

**Errors**: `404` if team not found, `400` if empty body.

---

## Detection & Analysis

### `POST /api/teams/:team_id/sweep`

Trigger a historical sweep. Runs the [`HistoricalAnalyzer`](../reconcile/analyzer.py) on the team's event timeline.

**Response** `200`
```json
{"status": "complete", "members": 5}
```

---

### `GET /api/teams/:team_id/alerts`

Paginated alert log.

**Query Parameters**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 100 | Max alerts to return |
| `severity` | string | — | Filter: `critical`, `suspect`, `elevated`, `info` |

**Response** `200`: Array of alert objects from the [`alerts`](../reconcile/storage.py) table.

---

### `GET /api/teams/:team_id/events`

Paginated event log (newest first).

**Query Parameters**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 100 | Max events to return |
| `since` | string | — | ISO timestamp filter |

**Response** `200`: Array of event objects from the [`events`](../reconcile/storage.py) table.

---

### `POST /api/teams/:team_id/inject`

Inject a synthetic event. For testing when live sources are down, or for replay.

**Request Body**
```json
{
  "action": "card.move",
  "actor": "alice",
  "target": "42",
  "target_type": "card",
  "source": "inject",
  "timestamp": "2026-01-15T14:30:00Z",
  "metadata": {"to_pipeline": "3659"}
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `action` | yes | Normalized action (e.g., `card.move`, `commit.create`) |
| `actor` | no | Actor name (resolved via `git_author_map` / `member_map`) |
| `target` | no | Target identifier |
| `timestamp` | no | ISO timestamp (default: now UTC) |
| `metadata` | no | Arbitrary metadata dict. Pipeline IDs resolved via `pipeline_map`. |

**Response** `200`
```json
{"status": "injected", "action": "card.move", "team_id": "team-a"}
```

---

## Streaming (SSE)

### `GET /api/alerts/stream`

Server-Sent Events stream of live alerts from all teams. Implemented in [`reconcile/web/sse.py`](../reconcile/web/sse.py).

**Headers**: `Content-Type: text/event-stream`

**Event format**
```
data: {"detector": "zero-commit-complete", "severity": "elevated", "category": "attribution", "score": 6, "title": "...", "detail": "...", "team_id": "team-a", "timestamp": "2026-01-15T14:30:00Z", "metadata": {}}
```

Persistent connection. Server pushes when alerts fire. Client uses `EventSource`.

---

### `GET /api/metrics/stream`

SSE stream of system metrics, pushed every 5 seconds. Reads from [`AlertCounters`](../reconcile/bus.py) (CQRS — zero DB queries).

**Event format**
```
data: {"uptime_seconds": 3600.5, "team_count": 3, "teams": {"team-a": {"name": "Alpha", "status": "running", "queue_depths": {"high": 0, "low": 12}, "timeline_size": 5000, "detectors": 8, "alerts": {"by_severity": {"critical": 2, "suspect": 5}, "by_category": {"evidence": 3}, "total": 47}}}}
```

---

## Integration

### `POST /hooks/github`

GitHub webhook receiver. Verifies HMAC signature via `X-Hub-Signature-256` if `GITHUB_WEBHOOK_SECRET` is configured.

Maps GitHub events to reconcile actions:

| GitHub Event | Action | Priority |
|-------------|--------|----------|
| `push` | `commit.push` | high |
| `create` | `branch.create` | high |
| `delete` | `branch.delete` | high |

Team resolution: `data.repository.name` → `orchestrator.repo_to_team` mapping.

**Response** `200`
```json
{"status": "ok"}
```

**Errors**: `403` if signature invalid, `400` if empty payload.

---

## System

### `GET /api/health`

Health check.

**Response** `200`
```json
{"status": "ok", "teams": 3}
```
