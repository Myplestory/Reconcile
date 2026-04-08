"""Tests for reconcile.detectors — anomaly detection + auto-discovery."""

import pytest
from datetime import datetime, timezone, timedelta

from reconcile.detectors import discover_detectors
from reconcile.detectors.zero_commit_complete import ZeroCommitCompleteDetector
from reconcile.detectors.branch_delete_complete import BranchDeleteCompleteDetector
from reconcile.detectors.batch_completion import BatchCompletionDetector
from reconcile.detectors.file_reattribution import FileReattributionDetector
from reconcile.detectors.completion_non_assignee import CompletionNonAssigneeDetector
from reconcile.detectors.unrecorded_deletion import UnrecordedDeletionDetector
from reconcile.detectors.report_revision import ReportRevisionDetector
from reconcile.detectors.attendance_anomaly import AttendanceAnomalyDetector
from .conftest import event_factory


# --- ZeroCommitComplete ---

@pytest.mark.asyncio
async def test_zero_commit_fires():
    d = ZeroCommitCompleteDetector()
    await d.detect(event_factory(action="card.tag", target="42", metadata={"tag": "branch:feat-x"}))
    alerts = await d.detect(event_factory(
        action="card.move", target="42", metadata={"to_pipeline_name": "Complete"},
    ))
    assert len(alerts) == 1
    assert "0 commits" in alerts[0].title


@pytest.mark.asyncio
async def test_zero_commit_no_fire_with_commits():
    d = ZeroCommitCompleteDetector()
    await d.detect(event_factory(action="card.tag", target="42", metadata={"tag": "branch:feat-x"}))
    await d.detect(event_factory(action="commit.create", source="git", target="abc", metadata={"branch": "feat-x"}))
    alerts = await d.detect(event_factory(
        action="card.move", target="42", metadata={"to_pipeline_name": "Complete"},
    ))
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_zero_commit_team_isolation():
    d = ZeroCommitCompleteDetector()
    # Team 1: tag branch
    await d.detect(event_factory(team_id="t1", action="card.tag", target="42", metadata={"tag": "branch:feat"}))
    # Team 2: commit on same branch name
    await d.detect(event_factory(team_id="t2", action="commit.create", source="git", metadata={"branch": "feat"}))
    # Team 1: complete — should still fire (t2's commit doesn't count for t1)
    alerts = await d.detect(event_factory(
        team_id="t1", action="card.move", target="42", metadata={"to_pipeline_name": "Done"},
    ))
    assert len(alerts) == 1


# --- BranchDeleteComplete ---

@pytest.mark.asyncio
async def test_branch_delete_complete_fires():
    d = BranchDeleteCompleteDetector(window_seconds=300)
    now = datetime.now(timezone.utc)
    await d.detect(event_factory(action="card.tag", target="42", metadata={"tag": "branch:feat"}))
    await d.detect(event_factory(action="branch.delete", target="feat", timestamp=now))
    alerts = await d.detect(event_factory(
        action="card.move", target="42", metadata={"to_pipeline_name": "Complete"},
        timestamp=now + timedelta(seconds=10),
    ))
    assert len(alerts) == 1
    assert alerts[0].severity == "critical"


@pytest.mark.asyncio
async def test_branch_delete_complete_outside_window():
    d = BranchDeleteCompleteDetector(window_seconds=60)
    now = datetime.now(timezone.utc)
    await d.detect(event_factory(action="card.tag", target="42", metadata={"tag": "branch:feat"}))
    await d.detect(event_factory(action="branch.delete", target="feat", timestamp=now))
    alerts = await d.detect(event_factory(
        action="card.move", target="42", metadata={"to_pipeline_name": "Complete"},
        timestamp=now + timedelta(seconds=120),  # outside window
    ))
    assert len(alerts) == 0


# --- BatchCompletion ---

@pytest.mark.asyncio
async def test_batch_completion_fires():
    d = BatchCompletionDetector(window_seconds=60, min_cards=3)
    now = datetime.now(timezone.utc)
    for i in range(3):
        alerts = await d.detect(event_factory(
            action="card.move", target=str(i),
            metadata={"to_pipeline_name": "Complete"},
            timestamp=now + timedelta(seconds=i),
        ))
    assert len(alerts) == 1
    assert "3 cards" in alerts[0].title


@pytest.mark.asyncio
async def test_batch_completion_below_threshold():
    d = BatchCompletionDetector(window_seconds=60, min_cards=3)
    now = datetime.now(timezone.utc)
    for i in range(2):
        alerts = await d.detect(event_factory(
            action="card.move", target=str(i),
            metadata={"to_pipeline_name": "Complete"},
            timestamp=now + timedelta(seconds=i),
        ))
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_batch_completion_prunes_old():
    d = BatchCompletionDetector(window_seconds=10, min_cards=3)
    now = datetime.now(timezone.utc)
    # First 2 events far in the past
    for i in range(2):
        await d.detect(event_factory(
            action="card.move", target=str(i),
            metadata={"to_pipeline_name": "Done"},
            timestamp=now - timedelta(seconds=60),
        ))
    # Third event now — old ones should be pruned
    alerts = await d.detect(event_factory(
        action="card.move", target="99",
        metadata={"to_pipeline_name": "Done"},
        timestamp=now,
    ))
    assert len(alerts) == 0


# --- FileReattribution ---

@pytest.mark.asyncio
async def test_file_reattribution_fires():
    d = FileReattributionDetector()
    await d.detect(event_factory(
        action="file.create", source="git", actor="alice", target="app.py",
        metadata={"content_hash": "abc123"},
    ))
    await d.detect(event_factory(action="file.delete", source="git", target="app.py"))
    alerts = await d.detect(event_factory(
        action="file.create", source="git", actor="bob", target="app.py",
        metadata={"content_hash": "abc123"},
    ))
    assert len(alerts) == 1
    assert alerts[0].severity == "suspect"
    assert "alice" in alerts[0].detail


@pytest.mark.asyncio
async def test_file_reattribution_same_author():
    d = FileReattributionDetector()
    await d.detect(event_factory(
        action="file.create", source="git", actor="alice", target="app.py",
        metadata={"content_hash": "abc123"},
    ))
    await d.detect(event_factory(action="file.delete", source="git", target="app.py"))
    alerts = await d.detect(event_factory(
        action="file.create", source="git", actor="alice", target="app.py",
        metadata={"content_hash": "abc123"},
    ))
    assert len(alerts) == 0


# --- CompletionNonAssignee ---

@pytest.mark.asyncio
async def test_completion_non_assignee_fires():
    d = CompletionNonAssigneeDetector()
    await d.detect(event_factory(action="card.assign", target="42", actor="alice", metadata={"member_id": "alice"}))
    alerts = await d.detect(event_factory(
        action="card.move", target="42", actor="bob",
        metadata={"to_pipeline_name": "Complete"},
    ))
    assert len(alerts) == 1
    assert "non-assignee" in alerts[0].title


@pytest.mark.asyncio
async def test_completion_non_assignee_pm_ok():
    d = CompletionNonAssigneeDetector()
    # Mark bob as PM
    await d.detect(event_factory(actor="bob", action="card.create", metadata={"is_pm": True}))
    await d.detect(event_factory(action="card.assign", target="42", actor="alice", metadata={"member_id": "alice"}))
    alerts = await d.detect(event_factory(
        action="card.move", target="42", actor="bob",
        metadata={"to_pipeline_name": "Done"},
    ))
    assert len(alerts) == 0


# --- UnrecordedDeletion ---

@pytest.mark.asyncio
async def test_unrecorded_deletion_fires():
    d = UnrecordedDeletionDetector()
    alerts = await d.detect(event_factory(
        action="branch.delete", source="git", target="feature-x",
    ))
    assert len(alerts) == 1
    assert "no board record" in alerts[0].title


@pytest.mark.asyncio
async def test_unrecorded_deletion_with_board_record():
    d = UnrecordedDeletionDetector()
    # Board records the unlink first
    await d.detect(event_factory(
        action="card.untag", source="board-ws", metadata={"tag": "branch:feature-x"},
    ))
    # Then git deletes
    alerts = await d.detect(event_factory(
        action="branch.delete", source="git", target="feature-x",
    ))
    assert len(alerts) == 0


# --- ReportRevision ---

@pytest.mark.asyncio
async def test_report_revision_fires():
    d = ReportRevisionDetector()
    now = datetime.now(timezone.utc)
    await d.detect(event_factory(
        action="report.submit", target="sprint-3", actor="alice",
        metadata={"period": "sprint-3", "markings": "alice:present,bob:absent"},
        timestamp=now,
    ))
    alerts = await d.detect(event_factory(
        action="report.submit", target="sprint-3", actor="alice",
        metadata={"period": "sprint-3", "markings": "alice:present,bob:present"},
        timestamp=now + timedelta(hours=1),
    ))
    assert len(alerts) == 1
    assert "revised" in alerts[0].title


@pytest.mark.asyncio
async def test_report_revision_same_markings_no_alert():
    d = ReportRevisionDetector()
    now = datetime.now(timezone.utc)
    await d.detect(event_factory(
        action="report.submit", target="sprint-3",
        metadata={"period": "sprint-3", "markings": "same"},
        timestamp=now,
    ))
    alerts = await d.detect(event_factory(
        action="report.submit", target="sprint-3",
        metadata={"period": "sprint-3", "markings": "same"},
        timestamp=now + timedelta(hours=1),
    ))
    assert len(alerts) == 0


# --- Auto-Discovery ---

# --- Attendance Anomaly ---

@pytest.mark.asyncio
async def test_attendance_present_with_activity():
    d = AttendanceAnomalyDetector(activity_window_minutes=120)
    now = datetime.now(timezone.utc)
    # Activity within window
    await d.detect(event_factory(actor="alice", action="commit.create", timestamp=now - timedelta(minutes=30)))
    # Marked present
    alerts = await d.detect(event_factory(actor="alice", action="session.present", timestamp=now, metadata={"member": "alice"}))
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_attendance_present_no_activity():
    d = AttendanceAnomalyDetector(activity_window_minutes=60)
    now = datetime.now(timezone.utc)
    # No activity at all → marked present
    alerts = await d.detect(event_factory(actor="alice", action="session.present", timestamp=now, metadata={"member": "alice"}))
    assert len(alerts) == 1
    assert alerts[0].severity == "elevated"
    assert "no corroborating activity" in alerts[0].title


@pytest.mark.asyncio
async def test_attendance_absent_with_notice():
    d = AttendanceAnomalyDetector(absence_comms_window_hours=24)
    now = datetime.now(timezone.utc)
    # Absence notice sent 2 hours before meeting
    await d.detect(event_factory(
        actor="alice", action="message.send",
        timestamp=now - timedelta(hours=2),
        metadata={"absence_notice": True},
    ))
    # Marked absent
    alerts = await d.detect(event_factory(actor="system", action="session.absent", timestamp=now, metadata={"member": "alice"}))
    assert len(alerts) == 1
    assert alerts[0].severity == "info"
    assert "prior notice" in alerts[0].title


@pytest.mark.asyncio
async def test_attendance_absent_no_notice():
    d = AttendanceAnomalyDetector()
    now = datetime.now(timezone.utc)
    alerts = await d.detect(event_factory(actor="system", action="session.absent", timestamp=now, metadata={"member": "alice"}))
    assert len(alerts) == 1
    assert alerts[0].severity == "elevated"
    assert "no prior communication" in alerts[0].title


@pytest.mark.asyncio
async def test_attendance_unexcused_streak_escalation():
    d = AttendanceAnomalyDetector(unexcused_absence_threshold=2)
    now = datetime.now(timezone.utc)
    # First unexcused → elevated
    alerts1 = await d.detect(event_factory(action="session.absent", timestamp=now, metadata={"member": "alice"}))
    assert alerts1[0].severity == "elevated"
    # Second unexcused → suspect (streak >= threshold)
    alerts2 = await d.detect(event_factory(action="session.absent", timestamp=now + timedelta(days=7), metadata={"member": "alice"}))
    assert alerts2[0].severity == "suspect"
    assert "streak: 2" in alerts2[0].title


@pytest.mark.asyncio
async def test_attendance_frequent_excused():
    d = AttendanceAnomalyDetector(frequent_absence_threshold=2)
    now = datetime.now(timezone.utc)
    for i in range(2):
        # Send notice then mark absent
        await d.detect(event_factory(
            actor="bob", action="message.send",
            timestamp=now + timedelta(days=7 * i) - timedelta(hours=1),
            metadata={"absence_notice": True},
        ))
        alerts = await d.detect(event_factory(
            action="session.absent",
            timestamp=now + timedelta(days=7 * i),
            metadata={"member": "bob"},
        ))
    # Second excused absence hits frequent threshold → elevated
    assert alerts[0].severity == "elevated"
    assert "2 times" in alerts[0].title


@pytest.mark.asyncio
async def test_attendance_team_isolation():
    d = AttendanceAnomalyDetector(activity_window_minutes=60)
    now = datetime.now(timezone.utc)
    # Activity for team-1
    await d.detect(event_factory(team_id="t1", actor="alice", action="commit.create", timestamp=now))
    # Present check for team-2 — should NOT see team-1's activity
    alerts = await d.detect(event_factory(team_id="t2", actor="alice", action="session.present", timestamp=now, metadata={"member": "alice"}))
    assert len(alerts) == 1  # no corroborating activity in team-2


@pytest.mark.asyncio
async def test_attendance_presence_resets_streak():
    d = AttendanceAnomalyDetector(unexcused_absence_threshold=2)
    now = datetime.now(timezone.utc)
    # Build streak
    await d.detect(event_factory(action="session.absent", timestamp=now, metadata={"member": "alice"}))
    # Present resets streak
    await d.detect(event_factory(action="session.present", timestamp=now + timedelta(days=7), metadata={"member": "alice"}))
    # Next absence starts from 0 again → elevated, not suspect
    alerts = await d.detect(event_factory(action="session.absent", timestamp=now + timedelta(days=14), metadata={"member": "alice"}))
    assert alerts[0].severity == "elevated"


# --- Auto-Discovery ---

def test_discover_finds_all_detectors():
    found = discover_detectors()
    expected = {
        "zero-commit-complete", "branch-delete-before-complete",
        "batch-completion", "file-reattribution",
        "completion-non-assignee", "unrecorded-deletion", "report-revision",
        "attendance-anomaly",
    }
    assert expected == set(found.keys())


# --- Eviction ---

def test_evict_team():
    d = ZeroCommitCompleteDetector()
    d.team_state("t1")["branch_commits"]["x"] = 5
    assert "t1" in d._state
    d.evict_team("t1")
    assert "t1" not in d._state
