# Detector Reference

All detectors inherit from [`BaseDetector`](../reconcile/detectors/base.py). Auto-discovered at startup via [`discover_detectors()`](../reconcile/detectors/__init__.py). State partitioned by `team_id` internally.

Each detector has a `category` that determines its weight in composite scoring (see [`docs/schema.md`](schema.md)).

---

## Categories

| Category | Weight | Meaning |
|----------|:------:|---------|
| `process` | 1 | Workflow deviation (batch completion, non-assignee action) |
| `attendance` | 2 | Presence/participation anomaly |
| `attribution` | 3 | Authorship integrity violation |
| `evidence` | 4 | Evidence destruction or tampering |

Composite score = `category_weight x severity_weight`. Range: 1 (process + info) to 16 (evidence + critical).

---

## zero-commit-complete

**Source:** [`reconcile/detectors/zero_commit_complete.py`](../reconcile/detectors/zero_commit_complete.py)

| Field | Value |
|-------|-------|
| Category | `attribution` |
| Trigger | `card.move` to a completion column |
| Watches for | Card completed with 0 commits on its linked branch |

**Configurable parameters:** None.

**Logic:** Tracks `card.tag` events with `branch:` prefixes to build card→branch mappings. Tracks `commit.create` events to count commits per branch. On `card.move` to a completion column (detected via [`is_complete_column()`](../reconcile/schema.py)), checks if the linked branch has 0 commits.

---

## branch-delete-before-complete

**Source:** [`reconcile/detectors/branch_delete_complete.py`](../reconcile/detectors/branch_delete_complete.py)

| Field | Value |
|-------|-------|
| Category | `evidence` |
| Trigger | `card.move` to completion after recent `branch.delete` |
| Watches for | Branch deleted within N seconds before card marked Complete |

**Configurable parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `window_seconds` | 300 | Time window to check for preceding branch deletions |

---

## batch-completion

**Source:** [`reconcile/detectors/batch_completion.py`](../reconcile/detectors/batch_completion.py)

| Field | Value |
|-------|-------|
| Category | `process` |
| Trigger | `card.move` to completion |
| Watches for | N+ cards completed by same actor within time window |

**Configurable parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `window_seconds` | 60 | Time window for rapid completion detection |
| `min_cards` | 3 | Minimum cards in window to trigger alert |

---

## file-reattribution

**Source:** [`reconcile/detectors/file_reattribution.py`](../reconcile/detectors/file_reattribution.py)

| Field | Value |
|-------|-------|
| Category | `attribution` |
| Trigger | `file.create` with `original_author` metadata |
| Watches for | File deleted and re-added byte-identical under different author |

**Configurable parameters:** None.

---

## completion-non-assignee

**Source:** [`reconcile/detectors/completion_non_assignee.py`](../reconcile/detectors/completion_non_assignee.py)

| Field | Value |
|-------|-------|
| Category | `process` |
| Trigger | `card.move` to completion |
| Watches for | Card completed by someone other than the assignee |

**Configurable parameters:** None.

---

## unrecorded-deletion

**Source:** [`reconcile/detectors/unrecorded_deletion.py`](../reconcile/detectors/unrecorded_deletion.py)

| Field | Value |
|-------|-------|
| Category | `evidence` |
| Trigger | `branch.delete` from git |
| Watches for | Branch deleted in git with no corresponding board unlink/delete event |

**Configurable parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `window_seconds` | 600 | Time window to look for matching board events |

---

## report-revision

**Source:** [`reconcile/detectors/report_revision.py`](../reconcile/detectors/report_revision.py)

| Field | Value |
|-------|-------|
| Category | `attendance` |
| Trigger | `report.submit` |
| Watches for | Status report revised with different accountability markings than prior submission |

**Configurable parameters:** None.

---

## attendance-anomaly

**Source:** [`reconcile/detectors/attendance_anomaly.py`](../reconcile/detectors/attendance_anomaly.py)

| Field | Value |
|-------|-------|
| Category | `attendance` |
| Trigger | `session.present`, `session.absent`, activity events |
| Watches for | Three checks (see below) |

**Checks:**

1. **Marked present, no corroborating activity.** Member marked present but no observable board, git, or message activity within the activity window.
2. **Absent with notice.** Info-level if infrequent; escalates to `elevated` if total absences exceed threshold.
3. **Absent without notice.** Elevated on first occurrence; escalates to `suspect` on consecutive streak.

**Configurable parameters:**

| Parameter | Default | Description |
|-----------|---------|-------------|
| `activity_window_minutes` | 120 | Minutes around meeting time to check for corroborating activity |
| `absence_comms_window_hours` | 24 | Hours before meeting to look for absence notices |
| `unexcused_absence_threshold` | 2 | Consecutive unexcused absences to escalate to `suspect` |
| `frequent_absence_threshold` | 3 | Total absences (with notice) to flag as frequent |

---

## Runtime Configuration

Detector parameters can be changed at runtime via [`PATCH /api/teams/:id/config`](api.md). Changes apply immediately to running detector instances — no restart required.

```bash
curl -X PATCH http://localhost:8080/api/teams/team-a/config \
  -H 'Content-Type: application/json' \
  -d '{"detectors": {"batch-completion": {"min_cards": 5}, "attendance-anomaly": {"enabled": false}}}'
```

## Custom Detectors

Drop a `.py` file in [`reconcile/detectors/`](../reconcile/detectors/). Implement [`BaseDetector`](../reconcile/detectors/base.py). Auto-discovered on startup. No registration or config change needed.

```python
from reconcile.detectors.base import BaseDetector
from reconcile.schema import Event, Alert

class MyDetector(BaseDetector):
    name = "my-custom-check"
    category = "process"  # or "attendance", "attribution", "evidence"

    async def detect(self, event: Event) -> list[Alert]:
        # your logic here
        return []
```
