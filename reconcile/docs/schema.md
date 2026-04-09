# Data Model

---

## Event

Source: [`reconcile/schema.py`](../reconcile/schema.py)

Frozen dataclass. Immutable after creation. Content-addressable via `event_hash`.

```python
@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime       # UTC, from source's time authority
    source: str               # "board-ws", "github", "discord", "email", "inject"
    team_id: str              # partition key — all routing based on this
    actor: str                # member name (resolved via member_map/git_author_map)
    action: str               # normalized: "card.move", "commit.create", etc.
    target: str               # card number, branch name, file path
    target_type: str          # "card", "branch", "file", "report", "session"
    metadata: dict            # source-specific fields (pipeline IDs, tags, etc.)
    raw: dict                 # original payload (audit trail)
    confidence: str           # "server-authoritative" or "client-reported"
    priority: str             # "high" or "low" → bus queue routing
```

### `event_hash` (property)

SHA-256 of `actor + action + target + timestamp.isoformat()`, truncated to 16 hex chars. Used for:
- Deduplication: `INSERT OR IGNORE` on `UNIQUE(event_hash)`
- Alert→event linking: `alerts.event_hash` references `events.event_hash`
- Idempotent replay: re-ingesting same events produces no duplicates

### Priority Routing

Events are routed to high or low priority bus queues based on `priority`:

| Priority | Actions | Queue Size |
|----------|---------|:----------:|
| `high` | `card.move`, `card.delete`, `commit.create`, `branch.delete`, `message.send`, ... | 5,000 |
| `low` | `card.update`, `session.join`, `session.presence`, `message.edit` | 50,000 |

See [`HIGH_PRIORITY_ACTIONS`](../reconcile/schema.py) and [`LOW_PRIORITY_ACTIONS`](../reconcile/schema.py).

### Action Vocabulary

Normalized action strings used across all sources:

| Domain | Actions |
|--------|---------|
| Cards | `card.move`, `card.create`, `card.delete`, `card.assign`, `card.unassign`, `card.tag`, `card.untag`, `card.link`, `card.update` |
| Git | `commit.create`, `commit.push`, `branch.create`, `branch.delete`, `pr.open`, `pr.merge`, `pr.close` |
| Messaging | `message.send`, `message.delete`, `message.edit` |
| Files | `file.create`, `file.delete` |
| Sessions | `session.join`, `session.users`, `session.presence`, `session.present`, `session.absent` |
| Reports | `report.submit` |

---

## Alert

Source: [`reconcile/schema.py`](../reconcile/schema.py)

```python
@dataclass
class Alert:
    detector: str             # detector name (e.g., "zero-commit-complete")
    severity: str             # "info", "elevated", "suspect", "critical"
    title: str                # human-readable summary
    detail: str               # detailed explanation
    team_id: str = ""         # set by bus on emit
    category: str = "process" # "process", "attendance", "attribution", "evidence"
    timestamp: datetime       # auto-set to now(UTC)
    event_ids: list = []      # related event hashes
    metadata: dict = {}       # detector-specific data
```

### `score` (property)

Composite score = `category_weight x severity_weight`. See [`composite_score()`](../reconcile/schema.py).

| | info (1) | elevated (2) | suspect (3) | critical (4) |
|---|:---:|:---:|:---:|:---:|
| **process (1)** | 1 | 2 | 3 | 4 |
| **attendance (2)** | 2 | 4 | 6 | 8 |
| **attribution (3)** | 3 | 6 | 9 | 12 |
| **evidence (4)** | 4 | 8 | 12 | 16 |

### Severity Levels

| Level | Meaning |
|-------|---------|
| `info` | Observable pattern, not necessarily problematic |
| `elevated` | Noteworthy deviation from expected behavior |
| `suspect` | Strong indicator of integrity violation |
| `critical` | Evidence destruction or clear attribution fraud |

---

## Category

Source: [`reconcile/schema.py`](../reconcile/schema.py)

```python
class Category:
    PROCESS = "process"           # weight 1
    ATTENDANCE = "attendance"     # weight 2
    ATTRIBUTION = "attribution"   # weight 3
    EVIDENCE = "evidence"         # weight 4
```

Detectors declare their category as a class attribute. See [`docs/detectors.md`](detectors.md).

---

## MemberProfile

Source: [`reconcile/analyzer.py`](../reconcile/analyzer.py)

Computed by the [`HistoricalAnalyzer`](../reconcile/analyzer.py) during a sweep. Stored in the `profiles` table with append-only versioning.

```python
@dataclass
class MemberProfile:
    member: str                          # member name
    direction: str = "neutral"           # "perpetrator", "victim", "mixed", "neutral"
    perpetrator_score: int = 0           # sum from perpetrator-type flags
    victim_score: int = 0                # sum from victim-type flags
    flags: list[dict] = []              # detected violations

    # Activity counters
    cards_completed: int = 0
    cards_completed_zero_commits: int = 0
    branches_deleted: int = 0
    files_reattributed_to: int = 0
    files_reattributed_from: int = 0
    commits: int = 0
    messages_sent: int = 0
    proactive_count: int = 0
    meetings_present: int = 0
    meetings_absent: int = 0
```

### Flag Structure

Each flag in `MemberProfile.flags`:

```json
{
  "type": "deleted-others-branch",
  "severity": "high",
  "actor": "bob",
  "victim": "alice",
  "date": "2026-01-15T14:30:00+00:00",
  "detail": "Deleted branch feat-login (authored by alice)"
}
```

Flag types: `zero-commit-completion`, `deleted-others-branch`, `branch-deleted-by-other`, `file-reattribution`, `file-reattributed-away`.

### Direction Classification

| Direction | Condition |
|-----------|-----------|
| `perpetrator` | `perpetrator_score > 0` and `victim_score == 0` |
| `victim` | `victim_score > 0` and `perpetrator_score == 0` |
| `mixed` | Both scores > 0 |
| `neutral` | Both scores == 0 |

---

## Column Detection

Source: [`is_complete_column()`](../reconcile/schema.py)

Normalized column name matching for completion detection. Case-insensitive, whitespace-trimmed.

Matches: `complete`, `done`, `finished`, `closed`, `resolved`, `merged`, `deployed`, `released`.

Used by detectors and the analyzer to identify when a card is moved to a completion column, regardless of the board tool's internal naming.
