"""Tests for reconcile.orchestrator — multi-team management."""

import asyncio
import pytest

from reconcile.orchestrator import Orchestrator, TeamConfig


def test_add_team_dict():
    orch = Orchestrator(db_path="/tmp/test_orch.db")
    orch.add_team(TeamConfig(team_id="t1", team_name="Alpha"))
    assert "t1" in orch.teams
    assert orch.teams["t1"].config.team_name == "Alpha"


def test_remove_team_cleanup():
    orch = Orchestrator(db_path="/tmp/test_orch.db")
    orch.add_team(TeamConfig(team_id="t1", github_repos=["repo-a"], board_id="100", discord_guild_id="g1"))
    assert orch.repo_to_team.get("repo-a") == "t1"
    assert orch.board_to_team.get("100") == "t1"
    orch.remove_team("t1")
    assert "t1" not in orch.teams
    assert "repo-a" not in orch.repo_to_team
    assert "100" not in orch.board_to_team
    assert "g1" not in orch.guild_to_team


def test_repo_to_team_mapping():
    orch = Orchestrator(db_path="/tmp/test_orch.db")
    orch.add_team(TeamConfig(team_id="t1", github_repos=["frontend", "backend"]))
    assert orch.repo_to_team["frontend"] == "t1"
    assert orch.repo_to_team["backend"] == "t1"


def test_subscribe_alerts_wired():
    orch = Orchestrator(db_path="/tmp/test_orch.db")
    orch.add_team(TeamConfig(team_id="t1"))
    queue = asyncio.Queue()
    orch.subscribe_alerts(queue)
    assert queue in orch.teams["t1"].bus._alert_subscribers


def test_store_wired_to_runner():
    orch = Orchestrator(db_path="/tmp/test_orch.db")
    orch.add_team(TeamConfig(team_id="t1"))
    assert orch.teams["t1"].store is orch._store
