"""Tests for reconcile.analyzer — historical sweep and scoring."""

import pytest
from datetime import datetime, timezone

from reconcile.analyzer import HistoricalAnalyzer
from .conftest import event_factory


@pytest.mark.asyncio
async def test_sweep_empty_timeline():
    a = HistoricalAnalyzer()
    profiles = await a.sweep([], "t1")
    assert profiles == {}


@pytest.mark.asyncio
async def test_sweep_basic_profile():
    a = HistoricalAnalyzer()
    timeline = [
        event_factory(actor="alice", action="commit.create", metadata={"branch": "main"}),
        event_factory(actor="alice", action="commit.create", metadata={"branch": "main"}),
    ]
    profiles = await a.sweep(timeline, "t1")
    assert profiles["alice"].commits == 2
    assert profiles["alice"].direction == "neutral"


@pytest.mark.asyncio
async def test_sweep_perpetrator_direction():
    a = HistoricalAnalyzer()
    now = datetime.now(timezone.utc)
    timeline = [
        event_factory(actor="alice", action="commit.create", metadata={"branch": "b1"}),
        event_factory(actor="bob", action="branch.delete", target="b1"),
    ]
    profiles = await a.sweep(timeline, "t1")
    assert profiles["bob"].direction == "perpetrator"
    assert profiles["bob"].perpetrator_score > 0


@pytest.mark.asyncio
async def test_sweep_victim_created_without_prior_events():
    """Victim profile should be created even if victim has no events of their own."""
    a = HistoricalAnalyzer()
    timeline = [
        # Alice commits to b1 (establishes authorship)
        event_factory(actor="alice", action="commit.create", metadata={"branch": "b1"}),
        # Bob deletes alice's branch (bob has no prior events)
        event_factory(actor="bob", action="branch.delete", target="b1"),
    ]
    profiles = await a.sweep(timeline, "t1")
    assert "alice" in profiles
    assert any(f["type"] == "branch-deleted-by-other" for f in profiles["alice"].flags)
    assert profiles["alice"].direction == "victim"


@pytest.mark.asyncio
async def test_sweep_mixed_direction():
    a = HistoricalAnalyzer()
    timeline = [
        event_factory(actor="alice", action="commit.create", metadata={"branch": "b1"}),
        event_factory(actor="bob", action="commit.create", metadata={"branch": "b2"}),
        # alice deletes bob's branch
        event_factory(actor="alice", action="branch.delete", target="b2"),
        # bob deletes alice's branch
        event_factory(actor="bob", action="branch.delete", target="b1"),
    ]
    profiles = await a.sweep(timeline, "t1")
    assert profiles["alice"].direction == "mixed"
    assert profiles["bob"].direction == "mixed"


@pytest.mark.asyncio
async def test_sweep_file_reattribution_scores():
    a = HistoricalAnalyzer()
    timeline = [
        event_factory(actor="bob", action="file.create", target="app.py",
                      metadata={"content_hash": "abc", "original_author": "alice"}),
    ]
    profiles = await a.sweep(timeline, "t1")
    assert profiles["bob"].files_reattributed_to == 1
    assert profiles["alice"].files_reattributed_from == 1


@pytest.mark.asyncio
async def test_sweep_complete_column_normalized():
    """Analyzer should use is_complete_column for 'Done', not just 'Complete'."""
    a = HistoricalAnalyzer()
    timeline = [
        event_factory(action="card.tag", target="42", metadata={"tag": "branch:feat"}),
        event_factory(action="card.move", target="42", metadata={"to_pipeline_name": "Done"}),
    ]
    profiles = await a.sweep(timeline, "t1")
    p = profiles["alice"]
    assert p.cards_completed == 1
    assert p.cards_completed_zero_commits == 1


@pytest.mark.asyncio
async def test_flags_have_structured_actor_victim():
    """Flags for branch deletion and file reattribution should include actor/victim."""
    a = HistoricalAnalyzer()
    timeline = [
        event_factory(actor="alice", action="commit.create", metadata={"branch": "b1"}),
        event_factory(actor="bob", action="branch.delete", target="b1"),
        event_factory(actor="carol", action="file.create", target="app.py",
                      metadata={"content_hash": "abc", "original_author": "alice"}),
    ]
    profiles = await a.sweep(timeline, "t1")

    # Bob's perpetrator flag
    bob_flags = [f for f in profiles["bob"].flags if f["type"] == "deleted-others-branch"]
    assert bob_flags[0]["actor"] == "bob"
    assert bob_flags[0]["victim"] == "alice"

    # Alice's victim flag
    alice_branch_flags = [f for f in profiles["alice"].flags if f["type"] == "branch-deleted-by-other"]
    assert alice_branch_flags[0]["actor"] == "bob"
    assert alice_branch_flags[0]["victim"] == "alice"

    # Carol's reattribution flag
    carol_flags = [f for f in profiles["carol"].flags if f["type"] == "file-reattribution"]
    assert carol_flags[0]["actor"] == "carol"
    assert carol_flags[0]["victim"] == "alice"

    # Alice's reattributed-away flag
    alice_file_flags = [f for f in profiles["alice"].flags if f["type"] == "file-reattributed-away"]
    assert alice_file_flags[0]["actor"] == "carol"
    assert alice_file_flags[0]["victim"] == "alice"


@pytest.mark.asyncio
async def test_sweep_messages_and_presence():
    a = HistoricalAnalyzer()
    timeline = [
        event_factory(actor="alice", action="message.send", metadata={"proactive": True}),
        event_factory(actor="alice", action="message.send"),
        event_factory(actor="alice", action="session.present"),
        event_factory(actor="alice", action="session.absent"),
    ]
    profiles = await a.sweep(timeline, "t1")
    assert profiles["alice"].messages_sent == 2
    assert profiles["alice"].proactive_count == 1
    assert profiles["alice"].meetings_present == 1
    assert profiles["alice"].meetings_absent == 1
