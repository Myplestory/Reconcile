"""Tests for reconcile.ingestors — Board WS and Git poll."""

import pytest
from unittest.mock import patch

from reconcile.ingestors.ws_board import BoardWSIngestor, DEFAULT_ACTION_MAP
from reconcile.ingestors.git_poll import GitPollIngestor
from reconcile.schema import default_priority


# --- BoardWSIngestor ---

def test_board_ws_normalize_move_card():
    ing = BoardWSIngestor("wss://test", source_name="myboard", default_team_id="t1")
    event = ing._normalize({
        "action": "moveCard",
        "cardid": "42",
        "pipelineid": "col-5",
        "oldpipelineid": "col-4",
        "activity": {"user_id": "123"},
    })
    assert event is not None
    assert event.action == "card.move"
    assert event.target == "42"
    assert event.team_id == "t1"
    assert event.source == "myboard"
    assert event.metadata["to_pipeline"] == "col-5"


def test_board_ws_normalize_unknown_action():
    ing = BoardWSIngestor("wss://test", default_team_id="t1")
    event = ing._normalize({"action": "unknownAction"})
    assert event is None


def test_board_ws_resolve_actor_member_map():
    ing = BoardWSIngestor("wss://test", member_map={"123": "Alice"}, default_team_id="t1")
    actor = ing._resolve_actor({"activity": {"user_id": "123"}})
    assert actor == "Alice"


def test_board_ws_resolve_actor_fallback():
    ing = BoardWSIngestor("wss://test", default_team_id="t1")
    actor = ing._resolve_actor({"userid": "999"})
    assert actor == "999"


def test_board_ws_resolve_team_id():
    ing = BoardWSIngestor("wss://test", board_team_map={"100": "team-a"}, default_team_id="fallback")
    assert ing._resolve_team_id({"boardid": "100"}) == "team-a"
    assert ing._resolve_team_id({"boardid": "999"}) == "fallback"


def test_board_ws_priority():
    ing = BoardWSIngestor("wss://test", default_team_id="t1")
    move = ing._normalize({"action": "moveCard", "cardid": "1"})
    join = ing._normalize({"action": "join", "userid": "1"})
    assert move.priority == "high"
    assert join.priority == "low"


def test_board_ws_custom_action_map():
    custom = {"task_done": ("card.move", "card")}
    ing = BoardWSIngestor("wss://test", action_map=custom, default_team_id="t1")
    event = ing._normalize({"action": "task_done", "cardid": "1"})
    assert event is not None
    assert event.action == "card.move"
    # Default action should NOT work
    assert ing._normalize({"action": "moveCard", "cardid": "1"}) is None


def test_board_ws_custom_board_id_field():
    ing = BoardWSIngestor(
        "wss://test",
        board_id_field="project_id",
        board_team_map={"p1": "team-x"},
        default_team_id="fallback",
    )
    assert ing._resolve_team_id({"project_id": "p1"}) == "team-x"


def test_board_ws_custom_card_id_field():
    ing = BoardWSIngestor(
        "wss://test",
        card_id_field="task_id",
        default_team_id="t1",
    )
    event = ing._normalize({"action": "moveCard", "task_id": "T-42"})
    assert event.target == "T-42"


def test_board_ws_custom_actor_resolver():
    def custom_resolver(msg):
        return msg.get("user", {}).get("display_name", "anon")

    ing = BoardWSIngestor("wss://test", actor_resolver=custom_resolver, default_team_id="t1")
    actor = ing._resolve_actor({"user": {"display_name": "Charlie"}})
    assert actor == "Charlie"


def test_board_ws_custom_metadata_extractor():
    def custom_meta(action, msg):
        return {"custom_field": msg.get("extra", "")}

    ing = BoardWSIngestor("wss://test", metadata_extractor=custom_meta, default_team_id="t1")
    event = ing._normalize({"action": "moveCard", "cardid": "1", "extra": "data"})
    assert event.metadata == {"custom_field": "data"}


# --- GitPollIngestor ---

def test_git_poll_initialized_flag():
    ing = GitPollIngestor("/nonexistent", team_id="t1")
    assert ing._initialized is False


def test_git_poll_subprocess_timeout():
    ing = GitPollIngestor("/nonexistent", team_id="t1")
    with patch("subprocess.run", side_effect=__import__("subprocess").TimeoutExpired("git", 30)):
        result = ing._git("log")
    assert result == ""


def test_git_poll_resolve_author():
    ing = GitPollIngestor("/tmp", team_id="t1", member_map={"John Doe": "john"})
    assert ing._resolve_author("John Doe") == "john"
    assert ing._resolve_author("Unknown") == "Unknown"
