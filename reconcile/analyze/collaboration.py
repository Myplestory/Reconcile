"""Collaboration health metrics — academically cited, defensible computations.

All metrics are pure functions over event lists and member sets. No side effects,
no database access. Caller provides data, gets back metric dicts.

Citations:
    Gini:          Mockus, Fielding & Herbsleb (2002), ACM TOSE
    Entropy:       Hassan (2009), ICSE
    Interaction:   Cataldo, Herbsleb et al. (2006/2008), CSCW/ESEM
    Bus Factor:    Avelino, Passos, Hora & Valente (2016), ICSE
    Cadence:       Claes, Mantyla et al. (2018), ICSE
    Lead Time:     Anderson (2010), Kanban
    Clustering:    Eyolfson, Tan & Lam (2011), MSR
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

# Board event types that represent intentional work (not passive browsing)
ACTION_EVENTS = frozenset({
    "card.move", "card.create", "card.delete", "card.assign", "card.unassign",
    "card.tag", "card.untag", "card.link", "card.unlink",
    "commit.create", "commit.push",
    "branch.create", "branch.delete",
    "message.send",
    "pr.open", "pr.merge",
    "report.submit",
})

# Passive noise events to exclude from all metric calculations
NOISE_ACTIONS = frozenset({
    "card.access", "board.load", "card.update",
    "session.join", "session.leave", "session.presence", "session.users",
})


def _filter_active_events(events: list[dict], members: set[str] | None = None) -> list[dict]:
    """Filter to action events only, optionally restricting to known members."""
    result = []
    for e in events:
        if e.get("action") in NOISE_ACTIONS:
            continue
        if e.get("action") not in ACTION_EVENTS:
            continue
        if members and e.get("actor") not in members:
            continue
        result.append(e)
    return result


# ---------------------------------------------------------------------------
# Metric 1: Gini Coefficient (work distribution inequality)
# Mockus, Fielding & Herbsleb (2002)
# ---------------------------------------------------------------------------

def gini_coefficient(contributions: list[int | float]) -> float:
    """Compute Gini coefficient. 0 = perfectly equal, 1 = maximally unequal.

    G = sum_i sum_j |x_i - x_j| / (2 * n^2 * x_bar)

    Handles edge cases: all zeros → 0.0, single member → 0.0.
    """
    n = len(contributions)
    if n < 2:
        return 0.0
    total = sum(contributions)
    if total == 0:
        return 0.0
    mean = total / n
    abs_diff_sum = sum(abs(xi - xj) for xi in contributions for xj in contributions)
    return abs_diff_sum / (2 * n * n * mean)


# ---------------------------------------------------------------------------
# Metric 2: Shannon Entropy (participation breadth)
# Hassan (2009), ICSE
# ---------------------------------------------------------------------------

def shannon_entropy(contributions: list[int | float]) -> float:
    """Normalized Shannon entropy. 1.0 = uniform, 0.0 = single contributor.

    H = -sum(p_i * log2(p_i)) for p_i > 0
    H_norm = H / log2(n)
    """
    n = len(contributions)
    if n < 2:
        return 0.0
    total = sum(contributions)
    if total == 0:
        return 0.0
    h = 0.0
    for c in contributions:
        if c > 0:
            p = c / total
            h -= p * math.log2(p)
    max_h = math.log2(n)
    if max_h == 0:
        return 0.0
    return h / max_h


# ---------------------------------------------------------------------------
# Metric 3: Interaction Density (Conway's Law graph)
# Cataldo, Herbsleb et al. (2006/2008)
# ---------------------------------------------------------------------------

def interaction_density(co_touches: dict[tuple[str, str], int], members: set[str]) -> float:
    """Graph density of member co-modification network.

    density = 2|E| / (|V| * (|V| - 1))

    co_touches: {(memberA, memberB): weight} — unordered pairs.
    """
    n = len(members)
    if n < 2:
        return 0.0
    edges = len(co_touches)
    max_edges = n * (n - 1) / 2
    return edges / max_edges if max_edges > 0 else 0.0


def compute_co_touches(events: list[dict], members: set[str]) -> dict[tuple[str, str], int]:
    """Compute co-touch edges: two members who both touched the same card.

    Returns {(A, B): count} where A < B lexicographically (deduped pairs).
    """
    card_members: dict[str, set[str]] = defaultdict(set)
    for e in events:
        actor = e.get("actor", "")
        target = e.get("target", "")
        if actor in members and target:
            card_members[target].add(actor)

    co_touch: dict[tuple[str, str], int] = defaultdict(int)
    for card, touchers in card_members.items():
        touchers_list = sorted(touchers)
        for i in range(len(touchers_list)):
            for j in range(i + 1, len(touchers_list)):
                pair = (touchers_list[i], touchers_list[j])
                co_touch[pair] += 1
    return dict(co_touch)


# ---------------------------------------------------------------------------
# Metric 4: Bus Factor
# Avelino, Passos, Hora & Valente (2016), ICSE
# ---------------------------------------------------------------------------

def bus_factor(ownership: dict[str, str], members: set[str]) -> int:
    """Compute bus factor: minimum removals before >50% of artifacts orphaned.

    ownership: {artifact: owner_member} — owner = member with most contributions.

    Simulates sequential removal of highest-ownership member.
    """
    if not ownership:
        return 0

    total_artifacts = len(ownership)
    threshold = total_artifacts * 0.5

    # Count artifacts per owner
    owner_counts: dict[str, int] = defaultdict(int)
    for artifact, owner in ownership.items():
        if owner in members:
            owner_counts[owner] += 1

    orphaned = 0
    removals = 0
    # Remove owners in descending order of ownership count
    for owner, count in sorted(owner_counts.items(), key=lambda x: -x[1]):
        orphaned += count
        removals += 1
        if orphaned > threshold:
            return removals

    return removals if removals > 0 else 1


# ---------------------------------------------------------------------------
# Metric 5: Commit Cadence Regularity
# Claes, Mantyla et al. (2018), ICSE
# ---------------------------------------------------------------------------

def cadence_regularity(daily_counts: list[int]) -> float:
    """Commit cadence regularity. Near 1.0 = steady, near 0 = bursty.

    regularity = 1 / (1 + CV)  where CV = sigma / mu

    Maps [0, inf) -> (0, 1]. Avoids negative values unlike 1-CV.
    """
    if not daily_counts:
        return 0.0
    n = len(daily_counts)
    mean = sum(daily_counts) / n
    if mean == 0:
        return 0.0
    variance = sum((x - mean) ** 2 for x in daily_counts) / n
    std = math.sqrt(variance)
    cv = std / mean
    return 1.0 / (1.0 + cv)


def compute_daily_commits(events: list[dict], start: datetime, end: datetime,
                          member: str | None = None) -> list[int]:
    """Count commits per day in the [start, end) window."""
    days = max(1, (end - start).days)
    counts = [0] * days
    for e in events:
        if e.get("action") != "commit.create":
            continue
        if member and e.get("actor") != member:
            continue
        ts = e.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
        if not isinstance(ts, datetime):
            continue
        day_idx = (ts - start).days
        if 0 <= day_idx < days:
            counts[day_idx] += 1
    return counts


# ---------------------------------------------------------------------------
# Metric 6: Lead Time & Deadline Clustering
# Anderson (2010), Kanban; Eyolfson, Tan & Lam (2011), MSR
# ---------------------------------------------------------------------------

def compute_lead_times(events: list[dict]) -> dict[str, float]:
    """Compute lead time (hours) per card: t_completed - t_created.

    Returns {card_id: hours}.
    """
    card_created: dict[str, datetime] = {}
    card_completed: dict[str, datetime] = {}

    for e in events:
        target = e.get("target", "")
        action = e.get("action", "")
        ts = e.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
        if not isinstance(ts, datetime):
            continue

        if action == "card.create":
            if target not in card_created:
                card_created[target] = ts
        elif action == "card.move":
            to_name = (e.get("metadata", {}).get("to_pipeline_name", "") or "").lower()
            if to_name in ("complete", "done", "closed"):
                card_completed[target] = ts

    result: dict[str, float] = {}
    for card_id, completed_ts in card_completed.items():
        created_ts = card_created.get(card_id)
        if created_ts:
            delta = (completed_ts - created_ts).total_seconds() / 3600
            if delta >= 0:
                result[card_id] = delta
    return result


def deadline_clustering_ratio(events: list[dict], deadline: datetime,
                              window_hours: int = 24) -> float:
    """Ratio of commits in final window before deadline vs total sprint commits.

    ratio > 0.4 = deadline-dependent, > 0.6 = cramming.
    """
    total = 0
    in_window = 0
    cutoff = deadline - timedelta(hours=window_hours)

    for e in events:
        if e.get("action") != "commit.create":
            continue
        ts = e.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
        if not isinstance(ts, datetime):
            continue
        total += 1
        if cutoff <= ts <= deadline:
            in_window += 1

    return in_window / total if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Metric 7: Assignment-Execution Mismatch
# ---------------------------------------------------------------------------

def assignment_mismatch(events: list[dict], members: set[str]) -> dict[str, dict]:
    """Track cards completed by non-assignee per member.

    Returns {member: {assigned: int, completed_by_other: int, mismatch_ratio: float}}.
    """
    card_assignee: dict[str, str] = {}
    card_completer: dict[str, str] = {}

    for e in events:
        action = e.get("action", "")
        target = e.get("target", "")
        actor = e.get("actor", "")
        if action == "card.assign":
            mid = e.get("metadata", {}).get("member_id", actor)
            card_assignee[target] = mid
        elif action == "card.move":
            to_name = (e.get("metadata", {}).get("to_pipeline_name", "") or "").lower()
            if to_name in ("complete", "done"):
                card_completer[target] = actor

    stats: dict[str, dict] = {}
    for m in members:
        assigned = sum(1 for a in card_assignee.values() if a == m)
        completed_by_other = sum(
            1 for card, assignee in card_assignee.items()
            if assignee == m and card in card_completer and card_completer[card] != m
        )
        ratio = completed_by_other / assigned if assigned > 0 else 0.0
        stats[m] = {"assigned": assigned, "completed_by_other": completed_by_other,
                     "mismatch_ratio": ratio}
    return stats


# ---------------------------------------------------------------------------
# Metric 8: Card Staleness
# ---------------------------------------------------------------------------

def stale_cards(events: list[dict], stale_days: int = 5,
                now: datetime | None = None) -> list[dict]:
    """Find cards in progress with no activity for >stale_days.

    Returns list of {card_id, last_activity, days_stale, last_actor}.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    card_last_activity: dict[str, tuple[datetime, str]] = {}
    card_pipeline: dict[str, str] = {}

    for e in events:
        target = e.get("target", "")
        action = e.get("action", "")
        actor = e.get("actor", "")
        if not target or action in NOISE_ACTIONS:
            continue

        ts = e.get("timestamp")
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
        if not isinstance(ts, datetime):
            continue

        card_last_activity[target] = (ts, actor)
        if action == "card.move":
            to_name = (e.get("metadata", {}).get("to_pipeline_name", "") or "").lower()
            if to_name:
                card_pipeline[target] = to_name

    # Ensure now is tz-aware for comparison
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    stale = []
    for card_id, (last_ts, last_actor) in card_last_activity.items():
        pipeline = card_pipeline.get(card_id, "")
        if pipeline in ("in progress", "testing"):
            # Handle mixed tz-awareness
            cmp_ts = last_ts if last_ts.tzinfo else last_ts.replace(tzinfo=timezone.utc)
            days = (now - cmp_ts).days
            if days >= stale_days:
                stale.append({
                    "card_id": card_id,
                    "last_activity": last_ts.isoformat(),
                    "days_stale": days,
                    "last_actor": last_actor,
                    "pipeline": pipeline,
                })
    return sorted(stale, key=lambda x: -x["days_stale"])


# ---------------------------------------------------------------------------
# Baseline & Trend Model
# ---------------------------------------------------------------------------

class TeamBaseline:
    """Rolling baseline per team. Updated each sprint."""

    def __init__(self):
        self.history: dict[str, list[float]] = {}

    def add_sprint(self, metrics: dict[str, float]) -> None:
        for name, val in metrics.items():
            self.history.setdefault(name, []).append(val)

    def baseline(self, metric: str) -> float:
        """Mean of all prior sprints (excludes current)."""
        vals = self.history.get(metric, [])
        prior = vals[:-1] if len(vals) > 1 else vals
        return sum(prior) / len(prior) if prior else 0.0

    def deviation(self, metric: str, current: float) -> float | None:
        """Z-score deviation from baseline. None if <3 prior sprints."""
        vals = self.history.get(metric, [])
        prior = vals[:-1] if len(vals) > 1 else vals
        if len(prior) < 3:
            return None
        mu = sum(prior) / len(prior)
        sigma = (sum((v - mu) ** 2 for v in prior) / (len(prior) - 1)) ** 0.5
        return (current - mu) / sigma if sigma > 0 else 0.0

    def trend(self, metric: str) -> str:
        """Direction via linear regression slope over last 4 sprints."""
        vals = self.history.get(metric, [])
        if len(vals) < 3:
            return "insufficient"
        recent = vals[-4:] if len(vals) >= 4 else vals
        n = len(recent)
        xs = list(range(n))
        x_bar = sum(xs) / n
        y_bar = sum(recent) / n
        num = sum((x - x_bar) * (y - y_bar) for x, y in zip(xs, recent))
        den = sum((x - x_bar) ** 2 for x in xs)
        slope = num / den if den > 0 else 0
        rel_slope = slope / y_bar if y_bar != 0 else 0
        if rel_slope > 0.05:
            return "increasing"
        elif rel_slope < -0.05:
            return "decreasing"
        return "stable"

    def to_dict(self) -> dict:
        return dict(self.history)

    @classmethod
    def from_dict(cls, data: dict) -> "TeamBaseline":
        bl = cls()
        bl.history = {k: list(v) for k, v in data.items()}
        return bl


# ---------------------------------------------------------------------------
# Composite Health Score
# ---------------------------------------------------------------------------

HEALTH_WEIGHTS = {
    "gini_inv": 0.20,
    "entropy": 0.15,
    "interaction": 0.15,
    "bus_factor": 0.15,
    "clustering_inv": 0.10,
    "cadence": 0.10,
    "churn_balance": 0.10,
    "attendance_corr": 0.05,
}


def composite_health_score(metrics: dict[str, float], n_members: int = 5) -> float:
    """Weighted composite health score, 0-100.

    Input metrics (all 0-1 where higher = healthier):
        gini_inv:      1 - gini
        entropy:        H_norm
        interaction:    density
        bus_factor:     bf / n_members (capped at 1)
        clustering_inv: 1 - clustering_ratio
        cadence:        regularity
        churn_balance:  1 - (other_churn / total_churn)
        attendance_corr: (r + 1) / 2  (maps [-1,1] to [0,1])
    """
    score = 0.0
    for key, weight in HEALTH_WEIGHTS.items():
        val = metrics.get(key, 0.5)  # default to neutral if missing
        score += weight * max(0.0, min(1.0, val))
    return round(score * 100, 1)


# ---------------------------------------------------------------------------
# Tier computation helper
# Mockus, Fielding & Herbsleb (2000): compute per artifact type because
# different contribution types have fundamentally different distributions.
# ---------------------------------------------------------------------------

GIT_ACTIONS = frozenset({
    "commit.create", "commit.push", "branch.create", "branch.delete",
    "file.create", "file.modify", "file.delete",
    "pr.open", "pr.merge",
})

BOARD_ACTIONS = frozenset({
    "card.move", "card.create", "card.delete", "card.assign", "card.unassign",
    "card.tag", "card.untag", "card.link", "card.unlink",
    "report.submit",
})


def _compute_tier_metrics(
    events: list[dict], members: set[str],
) -> dict[str, Any]:
    """Compute distributional metrics (Gini, entropy, density, bus factor)
    for a given event subset and member set.

    Returns dict with gini, entropy_norm, interaction_density, bus_factor, per_member counts.
    """
    member_counts: dict[str, int] = defaultdict(int)
    for e in events:
        actor = e.get("actor", "")
        if actor in members:
            member_counts[actor] += 1

    contributions = [member_counts.get(m, 0) for m in sorted(members)]
    active_members = [m for m in members if member_counts.get(m, 0) > 0]

    gini = gini_coefficient(contributions)
    active_contribs = [c for c in contributions if c > 0]
    entropy = shannon_entropy(active_contribs) if len(active_contribs) >= 2 else 0.0

    co_touches = compute_co_touches(events, members)
    density = interaction_density(co_touches, set(active_members) if active_members else members)

    card_touch_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for e in events:
        actor = e.get("actor", "")
        target = e.get("target", "")
        if actor in members and target:
            card_touch_counts[target][actor] += 1
    ownership = {c: max(t, key=t.get) for c, t in card_touch_counts.items() if t}
    bf = bus_factor(ownership, members)

    per_member = {}
    for m in sorted(members):
        per_member[m] = {"actions": member_counts.get(m, 0)}

    return {
        "gini": round(gini, 4),
        "entropy_norm": round(entropy, 4),
        "interaction_density": round(density, 4),
        "bus_factor": bf,
        "per_member": per_member,
    }


# ---------------------------------------------------------------------------
# PM Accountability (Cataldo et al. 2006)
# ---------------------------------------------------------------------------

def check_pm_accountability(
    baseline: "TeamBaseline",
    current_git_gini: float,
    pm_member: str,
    pm_assigns_this_sprint: int,
    sprint_number: int,
    zero_git_members: list[str] | None = None,
) -> list[dict]:
    """Flag PM when work distribution worsens under their oversight.

    Cataldo et al. (2006): PM is coordination node. Rising Gini with
    no corrective card.assign activity = coordination failure.

    Returns list of alert-like dicts {severity, title, detail}.
    """
    alerts: list[dict] = []

    trend = baseline.trend("git_gini")
    if trend == "increasing" and sprint_number >= 2:
        severity = "info" if sprint_number == 2 else "elevated"
        detail = (
            f"Git Gini trend is increasing (sprint {sprint_number}). "
            f"PM card.assign actions this sprint: {pm_assigns_this_sprint}. "
        )
        if pm_assigns_this_sprint == 0:
            detail += "No redistribution activity observed."
        alerts.append({
            "severity": severity,
            "title": "Work concentration increasing under PM oversight",
            "detail": detail,
        })

    # Flag disengaged members
    if zero_git_members:
        for m in zero_git_members:
            # Check if this member had 0 in prior sprint too
            hist = baseline.history.get(f"git_actions_{m}", [])
            consecutive_zeros = 0
            for v in reversed(hist):
                if v == 0:
                    consecutive_zeros += 1
                else:
                    break
            if consecutive_zeros >= 1:  # 0 last sprint + 0 this sprint = 2 consecutive
                alerts.append({
                    "severity": "elevated",
                    "title": f"Member {m} disengaged for {consecutive_zeros + 1} sprints",
                    "detail": (
                        f"{m} has 0 git activity for {consecutive_zeros + 1} consecutive sprints. "
                        f"No PM intervention (reassignment, check-in) observed."
                    ),
                })

    return alerts


# ---------------------------------------------------------------------------
# Orchestrator: compute all metrics for a sprint
# ---------------------------------------------------------------------------

def compute_collaboration_metrics(
    events: list[dict],
    members: set[str],
    sprint_start: datetime | None = None,
    sprint_end: datetime | None = None,
    git_churn: dict | None = None,
    pipeline_map: dict[str, str] | None = None,
    pm_member: str | None = None,
    commit_classifications: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Compute all collaboration metrics for a sprint window.

    Three tiers per Mockus et al. (2000):
        combined — all sources, all members (total participation)
        board    — card management events, all members (PM/coordination work)
        git      — code events, PM excluded from denominators (code contribution)

    events: list of event dicts (from storage or timeline).
    members: set of canonical member names.
    pm_member: canonical PM name. Excluded from git-tier denominators.
    sprint_start/end: window boundaries. If None, uses full event range.
    git_churn: pre-computed {member: {self_churn, other_churn}} from git_churn module.

    Returns flat dict with backward-compatible fields + tiered breakdown.
    """
    active_events = _filter_active_events(events, members)

    # Auto-derive sprint window from event timestamps if not provided
    if sprint_start is None or sprint_end is None:
        timestamps = []
        for e in active_events:
            ts = e.get("timestamp")
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts)
                except (ValueError, TypeError):
                    continue
            if isinstance(ts, datetime):
                timestamps.append(ts)
        if timestamps:
            if sprint_start is None:
                sprint_start = min(timestamps)
            if sprint_end is None:
                sprint_end = max(timestamps)

    # Split events by source type
    board_events = [e for e in active_events if e.get("action", "") in BOARD_ACTIONS]
    git_events = [e for e in active_events if e.get("action", "") in GIT_ACTIONS]

    # Git-tier members: exclude PM (their job is coordination, not code)
    git_members = members - {pm_member} if pm_member else members

    # Compute three tiers
    tier_combined = _compute_tier_metrics(active_events, members)
    tier_board = _compute_tier_metrics(board_events, members)
    tier_git = _compute_tier_metrics(git_events, git_members)
    # Git interaction_density is structurally N/A — commit SHAs are unique targets,
    # co-touch is undefined. Per Cataldo et al. (2006): interaction density measures
    # task-level coordination, not commit-level. Suppress to avoid misleading 0.0.
    tier_git["interaction_density"] = None

    # Use combined for backward-compatible top-level fields
    gini = tier_combined["gini"]
    entropy = tier_combined["entropy_norm"]
    density = tier_combined["interaction_density"]
    bf = tier_combined["bus_factor"]

    # Cadence (git-only by definition)
    if sprint_start and sprint_end:
        daily = compute_daily_commits(active_events, sprint_start, sprint_end)
        cadence = cadence_regularity(daily)
    else:
        cadence = 0.0
        daily = []

    # Lead times (board-only by definition)
    lead_times = compute_lead_times(events)

    # Clustering (git-only — commit-based)
    clustering = 0.0
    if sprint_end:
        clustering = deadline_clustering_ratio(active_events, sprint_end)

    # Churn balance
    churn_balance = 0.5
    if git_churn:
        total_self = sum(m.get("self_churn", 0) for m in git_churn.values())
        total_other = sum(m.get("other_churn", 0) for m in git_churn.values())
        total = total_self + total_other
        if total > 0:
            churn_balance = 1.0 - (total_other / total)

    # Assignment mismatch + stale cards (board-only)
    mismatch = assignment_mismatch(events, members)
    stale = stale_cards(events, now=sprint_end)

    # Per-member breakdown (combined view with split counts)
    per_member: dict[str, dict] = {}
    member_counts: dict[str, int] = defaultdict(int)
    for e in active_events:
        actor = e.get("actor", "")
        if actor in members:
            member_counts[actor] += 1

    for m in sorted(members):
        m_commits = sum(1 for e in git_events if e.get("actor") == m)
        m_board = sum(1 for e in board_events if e.get("actor") == m)
        m_cadence = 0.0
        if sprint_start and sprint_end:
            m_daily = compute_daily_commits(active_events, sprint_start, sprint_end, member=m)
            m_cadence = cadence_regularity(m_daily)

        per_member[m] = {
            "total_actions": member_counts.get(m, 0),
            "commits": m_commits,
            "board_actions": m_board,
            "cadence": m_cadence,
            "mismatch": mismatch.get(m, {}),
            "churn": git_churn.get(m, {}) if git_churn else {},
            "is_pm": m == pm_member if pm_member else False,
        }

    # Interaction graph (combined)
    co_touches = compute_co_touches(active_events, members)

    # Composite health (uses git-tier Gini — the signal that matters for CS course)
    health_inputs = {
        "gini_inv": 1.0 - tier_git["gini"],
        "entropy": tier_git["entropy_norm"],
        "interaction": density,
        "bus_factor": min(tier_git["bus_factor"] / max(len(git_members), 1), 1.0),
        "clustering_inv": 1.0 - clustering,
        "cadence": cadence,
        "churn_balance": churn_balance,
    }
    health = composite_health_score(health_inputs, len(git_members))

    result = {
        # Backward-compatible top-level (combined values)
        "gini": round(gini, 4),
        "entropy_norm": round(entropy, 4),
        "interaction_density": round(density, 4),
        "bus_factor": bf,
        "clustering_ratio": round(clustering, 4),
        "cadence_regularity": round(cadence, 4),
        "churn_balance": round(churn_balance, 4),
        "health_score": health,
        # Three-tier breakdown (Mockus et al. 2000)
        "tiers": {
            "combined": tier_combined,
            "board": tier_board,
            "git": tier_git,
        },
        "pm_member": pm_member,
        # Per-member and detail
        "per_member": per_member,
        "interaction_graph": {
            "nodes": sorted(m for m in members if member_counts.get(m, 0) > 0) or sorted(members),
            "edges": [{"source": a, "target": b, "weight": w}
                      for (a, b), w in sorted(co_touches.items())],
        },
        "lead_time_detail": {
            "cards": lead_times,
            "median_hours": _median(list(lead_times.values())) if lead_times else 0,
            "count": len(lead_times),
        },
        "cadence_detail": {
            "daily_commits": daily,
        },
        "stale_cards": stale,
        "assignment_mismatch": mismatch,
    }

    # NLI commit classification aggregation (v2)
    # Must be after result dict is assigned (not inline return)
    if commit_classifications:
        team_totals: dict[str, int] = defaultdict(int)
        per_member_cls: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for e in active_events:
            if e.get("action") == "commit.create":
                sha = str(e.get("target", ""))
                cls = commit_classifications.get(sha, {})
                category = cls.get("classification", "other")
                actor = e.get("actor", "")
                if actor:
                    team_totals[category] += 1
                    per_member_cls[actor][category] += 1
        # Add convenience fields
        for member_name, cats in per_member_cls.items():
            numeric = {k: v for k, v in cats.items() if isinstance(v, int)}
            if numeric:
                cats["primary_type"] = max(numeric, key=numeric.get)
                cats["total_classified"] = sum(numeric.values())
        result["commit_classifications"] = {
            "team_totals": dict(team_totals),
            "per_member": {m: dict(c) for m, c in per_member_cls.items()},
        }

    return result


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]
