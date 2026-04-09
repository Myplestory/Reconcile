"""Board activity ingest — events and cards from project management tool JSON."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

from ..config import PipelineConfig
from ..normalize.types import Event, Card


def _parse_branch_ref(detail: str) -> str | None:
    """Extract branch name from board activity detail string."""
    if not detail:
        return None
    if detail.startswith("branch:"):
        ref = detail[7:]
        if "github.com" in ref:
            if "/tree/" in ref:
                return ref.split("/tree/")[-1].lstrip("#")
            if "/pull/" in ref:
                return f"PR#{ref.split('/pull/')[-1]}"
            return ref
        return ref.lstrip("#")
    if detail.startswith("Removed Branch"):
        return detail[14:].lstrip("#")
    return None


def _resolve_user(uid: int, name: str, config: PipelineConfig) -> str:
    """Resolve a board user ID to canonical name."""
    return config.board_user_map.get(uid, config.uid_name_map.get(uid, name))


def _parse_timestamp(ts: str) -> datetime:
    """Parse board timestamp (YYYY-MM-DD HH:MM:SS, assumed US Eastern)."""
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.min


def load(config: PipelineConfig) -> tuple[list[Event], list[Card]]:
    """Load board activity JSON. Returns (events, cards).

    Also stores raw artifacts on load._raw for downstream analysis.
    """
    with open(config.sources.board_json) as f:
        raw = json.load(f)

    activities = raw.get("activity", raw) if isinstance(raw, dict) else raw
    team_uids = set(config.board_user_map.keys())

    events = []
    cards_dict: dict[int, dict] = defaultdict(lambda: {
        "created_by": None, "created_date": None,
        "members": [], "branches": [], "moves": [],
        "deletions": [], "tags": [], "comments": [],
        "title": "",
    })

    for a in activities:
        at = a.get("activity_type", "")
        uid = a.get("user_id")
        cn = a.get("card_number")
        who = _resolve_user(uid, a.get("username", ""), config)
        when_str = a.get("create_date", "")
        when = _parse_timestamp(when_str)
        detail = a.get("activity_detail", "") or ""

        # Skip noise
        if at in config.noise_types:
            continue

        # Build Event
        events.append(Event(
            timestamp=when,
            source="board",
            actor=who,
            action=at,
            entity_id=str(cn) if cn else "",
            detail=detail[:120],
            raw=a,
        ))

        # Build card lifecycle
        if not cn:
            continue
        card = cards_dict[cn]
        if not card["title"]:
            card["title"] = a.get("card_name", "")

        if at == "addcard":
            card["created_by"] = who
            card["created_date"] = when_str
        elif at == "addmember":
            m = re.search(r"member:(\d+)", detail)
            if m:
                member_uid = int(m.group(1))
                card["members"].append({
                    "action": "add", "uid": member_uid,
                    "name": _resolve_user(member_uid, str(member_uid), config),
                    "by": who, "date": when_str,
                })
        elif at == "delmember":
            m = re.search(r"member:(\d+)", detail)
            if m:
                member_uid = int(m.group(1))
                card["members"].append({
                    "action": "remove", "uid": member_uid,
                    "name": _resolve_user(member_uid, str(member_uid), config),
                    "by": who, "date": when_str,
                })
        elif at == "addgithub":
            branch = _parse_branch_ref(detail)
            card["branches"].append({"action": "add", "branch": branch, "by": who, "date": when_str})
        elif at == "delgithub":
            branch = _parse_branch_ref(detail)
            card["branches"].append({"action": "remove", "branch": branch, "by": who, "date": when_str})
        elif at == "moved":
            pm = re.search(r"pipeline\s*:\s*(.+?)(?:\s+on\s|$)", detail)
            pipeline_raw = pm.group(1).strip() if pm else detail
            pipeline = config.pipeline_map.get(pipeline_raw, pipeline_raw)
            card["moves"].append({"pipeline": pipeline, "by": who, "date": when_str})
        elif at == "deleteCard":
            card["deletions"].append({"by": who, "date": when_str})
        elif at in ("tagged", "untagged"):
            card["tags"].append({"action": at, "detail": detail, "by": who, "date": when_str})
        elif at in ("linked", "unlinked"):
            card["tags"].append({"action": at, "detail": detail, "by": who, "date": when_str})

    # Convert to Card dataclasses
    cards = []
    for num, data in cards_dict.items():
        assigned = list({m["name"] for m in data["members"] if m["action"] == "add"})
        branch_names = list({b["branch"] for b in data["branches"] if b["action"] == "add" and b["branch"]})

        cards.append(Card(
            number=num,
            title=data["title"],
            created_by=data["created_by"] or "",
            created_date=_parse_timestamp(data["created_date"]) if data["created_date"] else datetime.min,
            assigned_to=assigned,
            branches=branch_names,
            raw=data,
        ))

    # Store raw for invariant checks
    load._raw = {
        "card_data": dict(cards_dict),
        "board_activities": activities,
    }

    return events, cards
