"""Board tool WebSocket ingestor. Connects to any board tool's live event stream.

Generic — works with any board tool that pushes JSON over WebSocket.
The action map, field extractors, and source name are all passed in.
No hardcoded URLs, tool names, or field names.

Falls back to reconnect loop if connection drops (503, network error).
Demultiplexes by board_id_field → team_id. One connection serves all teams.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Callable

from reconcile.schema import Event, default_priority
from .base import BaseIngestor

log = logging.getLogger(__name__)

# Default action map — covers common board tool WebSocket actions.
# Override via constructor for tools with different action names.
DEFAULT_ACTION_MAP: dict[str, tuple[str, str]] = {
    "addcard":     ("card.create",    "card"),
    "updatecard":  ("card.update",    "card"),
    "delcard":     ("card.delete",    "card"),
    "addTag":      ("card.tag",       "card"),
    "delTag":      ("card.untag",     "card"),
    "addMember":   ("card.assign",    "card"),
    "delMember":   ("card.unassign",  "card"),
    "moveCard":    ("card.move",      "card"),
    "join":        ("session.join",   "session"),
    "updateusers": ("session.users",  "session"),
    "addDep":      ("card.link",      "card"),
}


class BoardWSIngestor(BaseIngestor):
    """Real-time board tool WebSocket ingestor.

    Generic — all tool-specific behavior is injected via constructor:
    - action_map: maps raw WS action names to normalized (action, target_type)
    - source_name: what to put in Event.source (e.g. "board-ws", "kanban")
    - actor_resolver: optional callable (msg_dict) → actor_str
    - team_resolver: optional callable (msg_dict) → team_id_str
    - metadata_extractor: optional callable (action, msg_dict) → metadata_dict

    Args:
        url: WebSocket endpoint (wss://...)
        source_name: Event.source value for events from this ingestor.
        action_map: {raw_action: (normalized_action, target_type)}.
                    Defaults to DEFAULT_ACTION_MAP.
        board_team_map: {board_id_str: team_id} for demultiplexing.
        member_map: {user_id_str: member_label} for actor resolution.
        board_id_field: JSON field name for board identifier (default: "boardid").
        card_id_field: JSON field name for card identifier (default: "cardid").
        default_team_id: team_id when board_team_map has no match.
        actor_resolver: Custom (msg) → actor function. Overrides member_map.
        metadata_extractor: Custom (action, msg) → metadata function.
        reconnect_delay: seconds between reconnect attempts.
    """

    def __init__(
        self,
        url: str,
        source_name: str = "board-ws",
        action_map: dict[str, tuple[str, str]] | None = None,
        board_team_map: dict[str, str] | None = None,
        member_map: dict[str, str] | None = None,
        board_id_field: str = "boardid",
        card_id_field: str = "cardid",
        default_team_id: str = "default",
        actor_resolver: Callable[[dict], str] | None = None,
        metadata_extractor: Callable[[str, dict], dict] | None = None,
        pipeline_map: dict[str, str] | None = None,
        reconnect_delay: float = 5.0,
    ):
        super().__init__()
        self.url = url
        self.source_name = source_name
        self.action_map = action_map or DEFAULT_ACTION_MAP
        self.board_team_map = board_team_map or {}
        self.member_map = member_map or {}
        self.board_id_field = board_id_field
        self.card_id_field = card_id_field
        self.default_team_id = default_team_id
        self._actor_resolver = actor_resolver
        self._metadata_extractor = metadata_extractor
        self.pipeline_map = pipeline_map or {}
        self.reconnect_delay = reconnect_delay

    def _resolve_actor(self, msg: dict) -> str:
        """Extract actor from WS message. Override via actor_resolver."""
        if self._actor_resolver:
            return self._actor_resolver(msg)
        # Default: check activity.user_id, then userid, then member_map
        activity = msg.get("activity", {})
        if isinstance(activity, dict):
            uid = str(activity.get("user_id", ""))
            if uid in self.member_map:
                return self.member_map[uid]
        uid = str(msg.get("userid", ""))
        if uid in self.member_map:
            return self.member_map[uid]
        return uid or "unknown"

    def _resolve_team_id(self, msg: dict) -> str:
        """Demux board event to team_id via board_id_field."""
        board_id = str(msg.get(self.board_id_field, msg.get("board_id", "")))
        return self.board_team_map.get(board_id, self.default_team_id)

    def _extract_metadata(self, action: str, msg: dict) -> dict:
        """Extract action-specific metadata. Override via metadata_extractor."""
        if self._metadata_extractor:
            return self._metadata_extractor(action, msg)
        # Default extraction for common board actions
        metadata: dict = {}
        if action == "card.move":
            to_pid = str(msg.get("pipelineid", ""))
            from_pid = str(msg.get("oldpipelineid", ""))
            metadata["from_pipeline"] = from_pid
            metadata["to_pipeline"] = to_pid
            # Resolve pipeline ID → name (for detector compatibility)
            metadata["to_pipeline_name"] = (
                self.pipeline_map.get(to_pid)
                or msg.get("pipeline_name", "")
            )
            metadata["position"] = msg.get("position", "")
        elif action in ("card.assign", "card.unassign"):
            metadata["member_id"] = msg.get("member_id", "")
        elif action in ("card.tag", "card.untag"):
            metadata["tag"] = msg.get("tagbody", msg.get("tag_id", ""))
        elif action == "card.link":
            metadata["dependency"] = msg.get("userset", "")
        return metadata

    def _normalize(self, msg: dict) -> Event | None:
        """Convert raw WS JSON to universal Event schema."""
        action_raw = msg.get("action", "")
        mapping = self.action_map.get(action_raw)
        if not mapping:
            log.debug("Unknown WS action: %s", action_raw)
            return None

        action, target_type = mapping
        target = str(msg.get(self.card_id_field, msg.get("card", {}).get("card_id", "")))

        return Event(
            timestamp=datetime.now(timezone.utc),
            source=self.source_name,
            team_id=self._resolve_team_id(msg),
            actor=self._resolve_actor(msg),
            action=action,
            target=target,
            target_type=target_type,
            metadata=self._extract_metadata(action, msg),
            raw=msg,
            confidence="server-authoritative",
            priority=default_priority(action),
        )

    async def stream(self) -> None:
        """Connect and stream events. Auto-reconnect on failure."""
        try:
            import websockets
        except ImportError:
            log.error("websockets package not installed. pip install websockets")
            return

        while True:
            try:
                log.info("Connecting to %s", self.url)
                async with websockets.connect(self.url) as ws:
                    log.info("Connected to board WebSocket (%s)", self.source_name)
                    async for raw_msg in ws:
                        try:
                            msg = json.loads(raw_msg)
                            event = self._normalize(msg)
                            if event:
                                await self.emit(event)
                                log.debug("Event: %s %s → %s", event.action, event.actor, event.target)
                        except json.JSONDecodeError:
                            log.warning("Non-JSON message: %s", raw_msg[:100])
                        except Exception as e:
                            log.warning("Failed to normalize WS message: %s (msg: %s)", e, str(raw_msg)[:200])

            except Exception as e:
                log.warning("WS connection failed: %s. Retry in %ss", e, self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)
