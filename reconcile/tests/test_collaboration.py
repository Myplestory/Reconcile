"""Tests for collaboration metrics module — formula correctness, edge cases, baseline model."""

import pytest
import math
from datetime import datetime, timezone, timedelta

from reconcile.analyze.collaboration import (
    gini_coefficient,
    shannon_entropy,
    interaction_density,
    bus_factor,
    cadence_regularity,
    composite_health_score,
    compute_co_touches,
    compute_lead_times,
    deadline_clustering_ratio,
    assignment_mismatch,
    stale_cards,
    compute_collaboration_metrics,
    TeamBaseline,
    _filter_active_events,
)


# --- Gini Coefficient ---

class TestGini:
    def test_all_equal(self):
        assert gini_coefficient([10, 10, 10, 10, 10]) == 0.0

    def test_one_dominant(self):
        g = gini_coefficient([100, 0, 0, 0, 0])
        assert abs(g - 0.8) < 0.01

    def test_zero_contributions(self):
        assert gini_coefficient([0, 0, 0, 0, 0]) == 0.0

    def test_single_member(self):
        assert gini_coefficient([50]) == 0.0

    def test_empty(self):
        assert gini_coefficient([]) == 0.0

    def test_two_members_unequal(self):
        g = gini_coefficient([90, 10])
        assert 0.3 < g < 0.5

    def test_monotonic_inequality(self):
        """More unequal distributions should have higher Gini."""
        g_even = gini_coefficient([25, 25, 25, 25])
        g_skewed = gini_coefficient([70, 20, 5, 5])
        g_extreme = gini_coefficient([100, 0, 0, 0])
        assert g_even < g_skewed < g_extreme


# --- Shannon Entropy ---

class TestEntropy:
    def test_uniform(self):
        h = shannon_entropy([10, 10, 10, 10])
        assert abs(h - 1.0) < 0.001

    def test_single_active(self):
        assert shannon_entropy([100]) == 0.0

    def test_skewed(self):
        h = shannon_entropy([99, 1])
        assert 0.0 < h < 0.5

    def test_empty(self):
        assert shannon_entropy([]) == 0.0

    def test_two_equal(self):
        h = shannon_entropy([50, 50])
        assert abs(h - 1.0) < 0.001

    def test_zero_safe(self):
        """p_i=0 should not cause log(0) error."""
        h = shannon_entropy([10, 0, 10])
        assert h > 0


# --- Interaction Density ---

class TestInteraction:
    def test_fully_connected(self):
        edges = {(f'm{i}', f'm{j}'): 1 for i in range(5) for j in range(i + 1, 5)}
        d = interaction_density(edges, {f'm{i}' for i in range(5)})
        assert abs(d - 1.0) < 0.001

    def test_no_edges(self):
        d = interaction_density({}, {f'm{i}' for i in range(5)})
        assert d == 0.0

    def test_single_member(self):
        assert interaction_density({}, {'m0'}) == 0.0

    def test_partial(self):
        edges = {('A', 'B'): 3}
        d = interaction_density(edges, {'A', 'B', 'C'})
        assert abs(d - 1/3) < 0.01


class TestCoTouches:
    def test_basic(self):
        events = [
            {"actor": "A", "target": "card1", "action": "card.move"},
            {"actor": "B", "target": "card1", "action": "card.create"},
            {"actor": "C", "target": "card2", "action": "card.move"},
        ]
        ct = compute_co_touches(events, {"A", "B", "C"})
        assert ("A", "B") in ct
        assert ct[("A", "B")] == 1
        assert ("A", "C") not in ct

    def test_dedup_pairs(self):
        """Same pair from different cards should accumulate."""
        events = [
            {"actor": "A", "target": "c1", "action": "card.move"},
            {"actor": "B", "target": "c1", "action": "card.move"},
            {"actor": "A", "target": "c2", "action": "card.move"},
            {"actor": "B", "target": "c2", "action": "card.move"},
        ]
        ct = compute_co_touches(events, {"A", "B"})
        assert ct[("A", "B")] == 2


# --- Bus Factor ---

class TestBusFactor:
    def test_single_owner(self):
        assert bus_factor({"f1": "A", "f2": "A", "f3": "A"}, {"A", "B", "C"}) == 1

    def test_even_split(self):
        bf = bus_factor({"f1": "A", "f2": "B", "f3": "C"}, {"A", "B", "C"})
        assert bf == 2  # need to remove 2 before >50% orphaned

    def test_empty(self):
        assert bus_factor({}, {"A"}) == 0


# --- Cadence Regularity ---

class TestCadence:
    def test_steady(self):
        c = cadence_regularity([5, 5, 5, 5, 5])
        assert abs(c - 1.0) < 0.001

    def test_bursty(self):
        c = cadence_regularity([0, 0, 0, 0, 25])
        assert c < 0.5

    def test_zero_mean(self):
        assert cadence_regularity([0, 0, 0]) == 0.0

    def test_empty(self):
        assert cadence_regularity([]) == 0.0

    def test_single_day(self):
        c = cadence_regularity([10])
        assert c > 0


# --- Baseline Model ---

class TestBaseline:
    def test_baseline_excludes_current(self):
        bl = TeamBaseline()
        for v in [0.4, 0.5, 0.6, 0.8]:
            bl.add_sprint({"gini": v})
        # Baseline should be mean of [0.4, 0.5, 0.6], excluding last
        b = bl.baseline("gini")
        assert abs(b - 0.5) < 0.001

    def test_insufficient_data(self):
        bl = TeamBaseline()
        bl.add_sprint({"x": 1.0})
        bl.add_sprint({"x": 2.0})
        assert bl.deviation("x", 2.0) is None

    def test_deviation_with_enough_data(self):
        bl = TeamBaseline()
        for v in [0.4, 0.5, 0.6, 0.7, 1.5]:
            bl.add_sprint({"gini": v})
        d = bl.deviation("gini", 1.5)
        assert d is not None
        assert d > 0  # 1.5 is above baseline

    def test_trend_increasing(self):
        bl = TeamBaseline()
        for v in [0.3, 0.5, 0.7, 0.9]:
            bl.add_sprint({"x": v})
        assert bl.trend("x") == "increasing"

    def test_trend_decreasing(self):
        bl = TeamBaseline()
        for v in [0.9, 0.7, 0.5, 0.3]:
            bl.add_sprint({"x": v})
        assert bl.trend("x") == "decreasing"

    def test_trend_stable(self):
        bl = TeamBaseline()
        for v in [0.5, 0.5, 0.5, 0.5]:
            bl.add_sprint({"x": v})
        assert bl.trend("x") == "stable"

    def test_trend_insufficient(self):
        bl = TeamBaseline()
        bl.add_sprint({"x": 0.5})
        assert bl.trend("x") == "insufficient"

    def test_serialization(self):
        bl = TeamBaseline()
        bl.add_sprint({"gini": 0.5, "entropy": 0.8})
        d = bl.to_dict()
        bl2 = TeamBaseline.from_dict(d)
        assert bl2.history == bl.history


# --- Composite Health Score ---

class TestHealth:
    def test_healthy_team(self):
        score = composite_health_score({
            "gini_inv": 0.8, "entropy": 0.9, "interaction": 0.7,
            "bus_factor": 0.6, "clustering_inv": 0.8, "cadence": 0.9,
            "churn_balance": 0.85,
        })
        assert score >= 70

    def test_unhealthy_team(self):
        score = composite_health_score({
            "gini_inv": 0.1, "entropy": 0.2, "interaction": 0.1,
            "bus_factor": 0.2, "clustering_inv": 0.3, "cadence": 0.1,
            "churn_balance": 0.2,
        })
        assert score < 40

    def test_missing_values_use_default(self):
        score = composite_health_score({})
        assert 40 <= score <= 60  # defaults to 0.5 for missing


# --- Lead Time ---

class TestLeadTime:
    def test_basic(self):
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        events = [
            {"action": "card.create", "target": "c1", "timestamp": now - timedelta(hours=48)},
            {"action": "card.move", "target": "c1", "timestamp": now,
             "metadata": {"to_pipeline_name": "Complete"}},
        ]
        lt = compute_lead_times(events)
        assert "c1" in lt
        assert abs(lt["c1"] - 48.0) < 0.01

    def test_no_create(self):
        """Cards without creation event should not appear."""
        events = [
            {"action": "card.move", "target": "c1",
             "timestamp": datetime(2026, 3, 1, tzinfo=timezone.utc),
             "metadata": {"to_pipeline_name": "Complete"}},
        ]
        lt = compute_lead_times(events)
        assert "c1" not in lt


# --- Deadline Clustering ---

class TestClustering:
    def test_all_last_day(self):
        deadline = datetime(2026, 3, 15, tzinfo=timezone.utc)
        events = [
            {"action": "commit.create", "timestamp": deadline - timedelta(hours=12)},
            {"action": "commit.create", "timestamp": deadline - timedelta(hours=6)},
            {"action": "commit.create", "timestamp": deadline - timedelta(hours=1)},
        ]
        r = deadline_clustering_ratio(events, deadline)
        assert r == 1.0

    def test_spread_out(self):
        deadline = datetime(2026, 3, 15, tzinfo=timezone.utc)
        events = [
            {"action": "commit.create", "timestamp": deadline - timedelta(days=10)},
            {"action": "commit.create", "timestamp": deadline - timedelta(days=7)},
            {"action": "commit.create", "timestamp": deadline - timedelta(days=3)},
            {"action": "commit.create", "timestamp": deadline - timedelta(hours=12)},
        ]
        r = deadline_clustering_ratio(events, deadline)
        assert r == 0.25

    def test_no_commits(self):
        deadline = datetime(2026, 3, 15, tzinfo=timezone.utc)
        assert deadline_clustering_ratio([], deadline) == 0.0


# --- Stale Cards ---

class TestStale:
    def test_stale_in_progress(self):
        now = datetime(2026, 3, 15, tzinfo=timezone.utc)
        events = [
            {"action": "card.move", "target": "c1", "actor": "A",
             "timestamp": now - timedelta(days=10),
             "metadata": {"to_pipeline_name": "In Progress"}},
        ]
        result = stale_cards(events, stale_days=5, now=now)
        assert len(result) == 1
        assert result[0]["card_id"] == "c1"
        assert result[0]["days_stale"] == 10

    def test_not_stale(self):
        now = datetime(2026, 3, 15, tzinfo=timezone.utc)
        events = [
            {"action": "card.move", "target": "c1", "actor": "A",
             "timestamp": now - timedelta(days=2),
             "metadata": {"to_pipeline_name": "In Progress"}},
        ]
        result = stale_cards(events, stale_days=5, now=now)
        assert len(result) == 0


# --- Filter ---

class TestFilter:
    def test_excludes_noise(self):
        events = [
            {"action": "card.access", "actor": "A"},
            {"action": "board.load", "actor": "A"},
            {"action": "card.move", "actor": "A"},
            {"action": "commit.create", "actor": "A"},
        ]
        filtered = _filter_active_events(events)
        assert len(filtered) == 2

    def test_member_filter(self):
        events = [
            {"action": "card.move", "actor": "A"},
            {"action": "card.move", "actor": "B"},
        ]
        filtered = _filter_active_events(events, members={"A"})
        assert len(filtered) == 1


# --- Integration: compute_collaboration_metrics ---

class TestComputeAll:
    def test_full_pipeline(self):
        now = datetime(2026, 3, 15, tzinfo=timezone.utc)
        start = now - timedelta(days=14)
        events = [
            {"timestamp": start + timedelta(days=1), "source": "git", "team_id": "t",
             "actor": "A", "action": "commit.create", "target": "abc", "target_type": "commit", "metadata": {}},
            {"timestamp": start + timedelta(days=3), "source": "board", "team_id": "t",
             "actor": "A", "action": "card.create", "target": "c1", "target_type": "card", "metadata": {}},
            {"timestamp": start + timedelta(days=5), "source": "board", "team_id": "t",
             "actor": "B", "action": "card.move", "target": "c1", "target_type": "card",
             "metadata": {"to_pipeline_name": "In Progress"}},
            {"timestamp": start + timedelta(days=10), "source": "git", "team_id": "t",
             "actor": "B", "action": "commit.create", "target": "def", "target_type": "commit", "metadata": {}},
            {"timestamp": start + timedelta(days=12), "source": "board", "team_id": "t",
             "actor": "A", "action": "card.move", "target": "c1", "target_type": "card",
             "metadata": {"to_pipeline_name": "Complete"}},
        ]
        members = {"A", "B"}
        result = compute_collaboration_metrics(events, members, start, now)

        assert "gini" in result
        assert "entropy_norm" in result
        assert "bus_factor" in result
        assert "health_score" in result
        assert "per_member" in result
        assert "interaction_graph" in result
        assert "lead_time_detail" in result
        assert result["per_member"]["A"]["commits"] == 1
        assert result["per_member"]["B"]["commits"] == 1
        assert result["lead_time_detail"]["count"] == 1

    def test_empty_events(self):
        result = compute_collaboration_metrics([], set(), None, None)
        assert result["gini"] == 0.0
        assert result["health_score"] > 0  # defaults to neutral

    def test_single_member(self):
        events = [
            {"timestamp": datetime(2026, 3, 1, tzinfo=timezone.utc), "source": "git",
             "team_id": "t", "actor": "A", "action": "commit.create",
             "target": "abc", "target_type": "commit", "metadata": {}},
        ]
        result = compute_collaboration_metrics(events, {"A"})
        assert result["gini"] == 0.0
        assert result["entropy_norm"] == 0.0

    def test_commit_classifications_aggregation(self):
        """NLI classification data flows into metrics when provided."""
        now = datetime(2026, 3, 15, tzinfo=timezone.utc)
        start = now - timedelta(days=14)
        events = [
            {"timestamp": start + timedelta(days=1), "source": "git", "team_id": "t",
             "actor": "A", "action": "commit.create", "target": "sha1",
             "target_type": "commit", "metadata": {}},
            {"timestamp": start + timedelta(days=2), "source": "git", "team_id": "t",
             "actor": "A", "action": "commit.create", "target": "sha2",
             "target_type": "commit", "metadata": {}},
            {"timestamp": start + timedelta(days=3), "source": "git", "team_id": "t",
             "actor": "B", "action": "commit.create", "target": "sha3",
             "target_type": "commit", "metadata": {}},
        ]
        classifications = {
            "sha1": {"classification": "feature", "confidence": 0.9},
            "sha2": {"classification": "maintenance:bugfix", "confidence": 0.8},
            "sha3": {"classification": "feature", "confidence": 0.95},
        }
        result = compute_collaboration_metrics(
            events, {"A", "B"}, start, now,
            commit_classifications=classifications,
        )
        assert "commit_classifications" in result
        cls = result["commit_classifications"]
        assert cls["team_totals"]["feature"] == 2
        assert cls["team_totals"]["maintenance:bugfix"] == 1
        assert cls["per_member"]["A"]["feature"] == 1
        assert cls["per_member"]["A"]["maintenance:bugfix"] == 1
        assert cls["per_member"]["A"]["primary_type"] in ("feature", "maintenance:bugfix")
        assert cls["per_member"]["B"]["feature"] == 1
        assert cls["per_member"]["B"]["primary_type"] == "feature"

    def test_no_classifications_no_key(self):
        """Without classifications param, no commit_classifications in result."""
        events = [
            {"timestamp": datetime(2026, 3, 1, tzinfo=timezone.utc), "source": "git",
             "team_id": "t", "actor": "A", "action": "commit.create",
             "target": "abc", "target_type": "commit", "metadata": {}},
        ]
        result = compute_collaboration_metrics(events, {"A"})
        assert "commit_classifications" not in result


# --- Column Flow Detector ---

class TestColumnFlowDetector:
    @pytest.mark.asyncio
    async def test_complete_without_testing(self):
        from reconcile.detectors.column_flow import ColumnFlowDetector
        from reconcile.schema import Event
        d = ColumnFlowDetector(pm_user_id="PM")
        e = Event(
            timestamp=datetime.now(timezone.utc), source="board", team_id="t",
            actor="dev", action="card.move", target="c1", target_type="card",
            metadata={"to_pipeline_name": "Complete", "from_pipeline": "In Progress"},
        )
        alerts = await d.detect(e)
        assert len(alerts) == 1
        assert "without testing" in alerts[0].title.lower()

    @pytest.mark.asyncio
    async def test_complete_from_testing_no_alert(self):
        from reconcile.detectors.column_flow import ColumnFlowDetector
        from reconcile.schema import Event
        d = ColumnFlowDetector(pm_user_id="PM")
        e = Event(
            timestamp=datetime.now(timezone.utc), source="board", team_id="t",
            actor="dev", action="card.move", target="c1", target_type="card",
            metadata={"to_pipeline_name": "Complete", "from_pipeline": "Testing"},
        )
        alerts = await d.detect(e)
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_backlog_regression(self):
        from reconcile.detectors.column_flow import ColumnFlowDetector
        from reconcile.schema import Event
        d = ColumnFlowDetector()
        e = Event(
            timestamp=datetime.now(timezone.utc), source="board", team_id="t",
            actor="dev", action="card.move", target="c1", target_type="card",
            metadata={"to_pipeline_name": "Backlog", "from_pipeline": "In Progress"},
        )
        alerts = await d.detect(e)
        assert len(alerts) == 1
        assert "regressed" in alerts[0].title.lower()

    @pytest.mark.asyncio
    async def test_closed_by_non_pm(self):
        from reconcile.detectors.column_flow import ColumnFlowDetector
        from reconcile.schema import Event
        d = ColumnFlowDetector(pm_user_id="PM")
        e = Event(
            timestamp=datetime.now(timezone.utc), source="board", team_id="t",
            actor="dev", action="card.move", target="c1", target_type="card",
            metadata={"to_pipeline_name": "Closed"},
        )
        alerts = await d.detect(e)
        assert len(alerts) == 1
        assert "non-PM" in alerts[0].title

    @pytest.mark.asyncio
    async def test_closed_by_pm_no_alert(self):
        from reconcile.detectors.column_flow import ColumnFlowDetector
        from reconcile.schema import Event
        d = ColumnFlowDetector(pm_user_id="PM")
        e = Event(
            timestamp=datetime.now(timezone.utc), source="board", team_id="t",
            actor="PM", action="card.move", target="c1", target_type="card",
            metadata={"to_pipeline_name": "Closed"},
        )
        alerts = await d.detect(e)
        assert len(alerts) == 0

    @pytest.mark.asyncio
    async def test_team_isolation(self):
        from reconcile.detectors.column_flow import ColumnFlowDetector
        from reconcile.schema import Event
        d = ColumnFlowDetector()
        # Set pipeline state for team-a
        e1 = Event(
            timestamp=datetime.now(timezone.utc), source="board", team_id="team-a",
            actor="dev", action="card.move", target="c1", target_type="card",
            metadata={"to_pipeline_name": "In Progress"},
        )
        await d.detect(e1)
        state_a = d.team_state("team-a")
        state_b = d.team_state("team-b")
        assert "c1" in state_a["card_pipeline"]
        assert "c1" not in state_b["card_pipeline"]
