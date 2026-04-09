"""Discord message ingest — load exported channel JSON files."""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone

from ..config import PipelineConfig
from ..normalize.types import Message

DISCORD_EPOCH_MS = 1420070400000


def snowflake_to_utc(sf_str: str) -> datetime:
    """Convert Discord Snowflake ID to UTC datetime."""
    ts_ms = (int(sf_str) >> 22) + DISCORD_EPOCH_MS
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)


def load(config: PipelineConfig) -> list[Message]:
    """Load all Discord messages from export directory."""
    discord_dir = config.sources.discord_dir
    if not discord_dir or not os.path.isdir(discord_dir):
        return []

    messages = []
    seen_ids: set[str] = set()

    for fpath in sorted(glob.glob(os.path.join(discord_dir, "*.json"))):
        fname = os.path.basename(fpath)
        if fname in ("export-summary.json", "snowflake-validation.json"):
            continue

        with open(fpath) as f:
            data = json.load(f)

        # Handle both formats: flat list or dict with "messages" key
        if isinstance(data, dict):
            cid = data.get("channel_id", "")
            cname = f"channel-{cid}" if cid else fname.rsplit("_", 1)[0]
            raw_msgs = data.get("messages", [])
        elif isinstance(data, list):
            cname = fname.rsplit("_", 1)[0]
            raw_msgs = data
        else:
            continue

        if not raw_msgs or not isinstance(raw_msgs[0], dict):
            continue

        for m in raw_msgs:
            msg_id = m.get("id", "")
            if not msg_id or msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            dt = snowflake_to_utc(msg_id)
            author = m.get("author", {})
            author_name = author.get("global_name") or author.get("username", "?")

            messages.append(Message(
                snowflake=msg_id,
                timestamp=dt,
                author=author_name,
                channel=cname,
                channel_id=m.get("channel_id", ""),
                content=m.get("content", "")[:300],
                raw=m,
            ))

    messages.sort(key=lambda m: m.timestamp)
    return messages
