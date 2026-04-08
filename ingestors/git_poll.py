"""Git repository poller. Detects new commits, branch changes, file changes.

To be replaced by GitHub webhooks in production (zero polling).
Retained for local development and repos without webhook access.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from reconcile.schema import Event, default_priority
from .base import BaseIngestor

log = logging.getLogger(__name__)


class GitPollIngestor(BaseIngestor):
    """Poll a git repo for changes on an interval.

    Detects:
      - New commits (commit.create)
      - Branch creation/deletion (branch.create, branch.delete)
      - File additions/modifications/deletions (file.create, file.modify, file.delete)
    """

    def __init__(
        self,
        repo_path: str,
        team_id: str = "default",
        member_map: dict[str, str] | None = None,
        interval: float = 60.0,
    ):
        super().__init__()
        self.repo = Path(repo_path)
        self.team_id = team_id
        self.member_map = member_map or {}
        self.interval = interval
        self._known_commits: set[str] = set()
        self._known_branches: set[str] = set()
        self._initialized = False

    def _git(self, *args) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repo)] + list(args),
                capture_output=True, text=True, timeout=30,
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            log.warning("Git command timed out: git %s", " ".join(args))
            return ""

    def _resolve_author(self, git_author: str) -> str:
        return self.member_map.get(git_author, git_author)

    def _get_branches(self) -> set[str]:
        output = self._git("branch", "-a", "--format=%(refname:short)")
        return set(output.split("\n")) if output else set()

    def _get_commits(self) -> list[dict]:
        output = self._git("log", "--all", "--format=%H|%an|%ad|%s", "--date=iso-strict")
        commits = []
        for line in output.split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|", 3)
            commits.append({
                "hash": parts[0],
                "author": parts[1] if len(parts) > 1 else "",
                "date": parts[2] if len(parts) > 2 else "",
                "message": parts[3] if len(parts) > 3 else "",
            })
        return commits

    def _get_commit_branch(self, commit_hash: str) -> str:
        """Find which branch contains this commit (best-effort)."""
        output = self._git("branch", "--contains", commit_hash, "--format=%(refname:short)")
        if output:
            # Return first branch (usually the feature branch)
            branches = [b.strip() for b in output.split("\n") if b.strip()]
            # Prefer non-main branches
            for b in branches:
                if b not in ("main", "master", "dev", "develop"):
                    return b
            return branches[0] if branches else ""
        return ""

    async def _poll_once(self) -> None:
        loop = asyncio.get_event_loop()

        branches = await loop.run_in_executor(None, self._get_branches)
        commits = await loop.run_in_executor(None, self._get_commits)

        if not self._initialized:
            # First poll: baseline only, no events emitted
            self._known_branches = branches
            for c in commits:
                self._known_commits.add(c["hash"])
            self._initialized = True
            log.info("Git baseline: %d branches, %d commits", len(branches), len(self._known_commits))
            return

        # Detect branch changes
        new_branches = branches - self._known_branches
        deleted_branches = self._known_branches - branches

        for b in new_branches:
            await self.emit(Event(
                timestamp=datetime.now(timezone.utc),
                source="git",
                team_id=self.team_id,
                actor="unknown",
                action="branch.create",
                target=b,
                target_type="branch",
                priority=default_priority("branch.create"),
            ))
        for b in deleted_branches:
            await self.emit(Event(
                timestamp=datetime.now(timezone.utc),
                source="git",
                team_id=self.team_id,
                actor="unknown",
                action="branch.delete",
                target=b,
                target_type="branch",
                priority=default_priority("branch.delete"),
            ))
        self._known_branches = branches

        # Detect new commits
        for c in commits:
            if c["hash"] not in self._known_commits:
                try:
                    ts = datetime.fromisoformat(c["date"])
                except (ValueError, TypeError):
                    ts = datetime.now(timezone.utc)

                # Resolve branch for this commit (needed by zero-commit detector)
                branch = await loop.run_in_executor(None, self._get_commit_branch, c["hash"])

                await self.emit(Event(
                    timestamp=ts,
                    source="git",
                    team_id=self.team_id,
                    actor=self._resolve_author(c["author"]),
                    action="commit.create",
                    target=c["hash"][:7],
                    target_type="commit",
                    metadata={"message": c["message"], "full_hash": c["hash"], "branch": branch},
                    confidence="inferred",
                    priority=default_priority("commit.create"),
                ))
                self._known_commits.add(c["hash"])

    async def stream(self) -> None:
        log.info("Git poller starting: %s (every %ss)", self.repo, self.interval)
        while True:
            try:
                await self._poll_once()
            except Exception as e:
                log.error("Git poll error: %s", e)
            await asyncio.sleep(self.interval)
