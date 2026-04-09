"""JSON file output. Append-only alert log."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from reconcile.schema import Alert


class JSONFileOutput:
    def __init__(self, path: str = "output/alerts.jsonl"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write_sync(self, line: str) -> None:
        with open(self.path, "a") as f:
            f.write(line)

    async def emit(self, alert: Alert) -> None:
        record = {
            "timestamp": alert.timestamp.isoformat(),
            "detector": alert.detector,
            "severity": alert.severity,
            "title": alert.title,
            "detail": alert.detail,
            "team_id": alert.team_id,
            "metadata": alert.metadata,
        }
        line = json.dumps(record, default=str) + "\n"
        await asyncio.get_event_loop().run_in_executor(None, self._write_sync, line)
