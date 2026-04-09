"""Detector: file deleted and re-added with identical content under different author."""

from __future__ import annotations

from reconcile.schema import Event, Alert
from .base import BaseDetector


class FileReattributionDetector(BaseDetector):
    name = "file-reattribution"
    description = "File re-added by different author with identical content (git blob match)"
    category = "attribution"

    def _init_team_state(self) -> dict:
        return {
            "known_files": {},    # file_path -> {author, content_hash, timestamp}
            "deleted_files": {},  # file_path -> {author, content_hash, timestamp}
        }

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)

        if event.action == "file.delete" and event.source == "git":
            path = event.target
            if path in s["known_files"]:
                s["deleted_files"][path] = s["known_files"].pop(path)

        if event.action == "file.create" and event.source == "git":
            path = event.target
            content_hash = event.metadata.get("content_hash", "")
            author = event.actor

            if path in s["deleted_files"]:
                prev = s["deleted_files"][path]
                if (content_hash and content_hash == prev.get("content_hash")
                        and author != prev.get("author")):
                    alerts.append(self.alert(
                        severity="suspect",
                        title=f"File {path} re-attributed: {prev['author']} → {author}",
                        detail=(
                            f"File '{path}' was originally authored by {prev['author']} "
                            f"(hash: {content_hash[:12]}). It was deleted and re-added by {author} "
                            f"with byte-identical content. git blame now shows {author}."
                        ),
                        team_id=event.team_id,
                        event_ids=[id(event)],
                    ))
                del s["deleted_files"][path]

            s["known_files"][path] = {
                "author": author,
                "content_hash": content_hash,
                "timestamp": event.timestamp.isoformat(),
            }

        return alerts
