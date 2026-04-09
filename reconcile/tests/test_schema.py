"""Tests for reconcile.schema — Event, Alert, action vocabulary."""

import pytest
from dataclasses import FrozenInstanceError
from reconcile.schema import (
    Event, Alert, default_priority, is_complete_column,
    HIGH_PRIORITY_ACTIONS, LOW_PRIORITY_ACTIONS,
)
from .conftest import event_factory


def test_event_frozen():
    e = event_factory()
    with pytest.raises(FrozenInstanceError):
        e.action = "card.delete"


def test_event_has_team_id():
    e = event_factory(team_id="t1")
    assert e.team_id == "t1"


def test_event_has_priority():
    e = event_factory(priority="low")
    assert e.priority == "low"


def test_event_defaults():
    e = event_factory()
    assert e.confidence == "server-authoritative"
    assert e.priority == "high"


def test_default_priority_high():
    for action in ["card.move", "card.delete", "commit.create", "branch.delete", "message.send"]:
        assert default_priority(action) == "high", f"{action} should be high"


def test_default_priority_low():
    for action in ["card.update", "session.join", "session.presence", "message.edit"]:
        assert default_priority(action) == "low", f"{action} should be low"


def test_default_priority_unknown():
    assert default_priority("unknown.action") == "high"


def test_is_complete_column_positive():
    for name in ["complete", "done", "finished", "closed", "resolved", "merged"]:
        assert is_complete_column(name), f"{name} should be complete"


def test_is_complete_column_negative():
    for name in ["in progress", "todo", "col-5", "", "backlog"]:
        assert not is_complete_column(name), f"{name} should not be complete"


def test_is_complete_column_case_insensitive():
    assert is_complete_column("COMPLETE")
    assert is_complete_column("Done")
    assert is_complete_column("  Complete  ")


def test_alert_team_id():
    a = Alert(detector="test", severity="info", title="t", detail="d", team_id="t1")
    assert a.team_id == "t1"


def test_event_hash_deterministic():
    from datetime import datetime, timezone
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    e1 = event_factory(actor="alice", action="card.move", target="42", timestamp=ts)
    e2 = event_factory(actor="alice", action="card.move", target="42", timestamp=ts)
    assert e1.event_hash == e2.event_hash


def test_event_hash_differs_on_content():
    e1 = event_factory(actor="alice", action="card.move", target="42")
    e2 = event_factory(actor="bob", action="card.move", target="42")
    assert e1.event_hash != e2.event_hash


def test_alert_category_default():
    a = Alert(detector="test", severity="info", title="t", detail="d")
    assert a.category == "process"


def test_alert_composite_score():
    from reconcile.schema import Category, composite_score
    # process + info = 1*1 = 1
    assert composite_score(Category.PROCESS, "info") == 1
    # evidence + critical = 4*4 = 16
    assert composite_score(Category.EVIDENCE, "critical") == 16
    # attribution + suspect = 3*3 = 9
    assert composite_score(Category.ATTRIBUTION, "suspect") == 9
    # attendance + elevated = 2*2 = 4
    assert composite_score(Category.ATTENDANCE, "elevated") == 4


def test_alert_score_property():
    a = Alert(detector="test", severity="critical", category="evidence", title="t", detail="d")
    assert a.score == 16


def test_detector_categories():
    """Each detector should declare a meaningful category."""
    from reconcile.detectors import discover_detectors
    from reconcile.schema import Category
    valid = {Category.PROCESS, Category.ATTENDANCE, Category.ATTRIBUTION, Category.EVIDENCE}
    for name, cls in discover_detectors().items():
        instance = cls() if name not in ("branch-delete-before-complete",) else cls(window_seconds=300)
        assert instance.category in valid, f"{name} has invalid category: {instance.category}"


def test_high_low_actions_disjoint():
    assert not HIGH_PRIORITY_ACTIONS & LOW_PRIORITY_ACTIONS
