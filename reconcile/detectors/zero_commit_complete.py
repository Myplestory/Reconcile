"""Detector: card completed with zero commits on its branch."""

from __future__ import annotations

from reconcile.schema import Event, Alert, is_complete_column
from .base import BaseDetector


class ZeroCommitCompleteDetector(BaseDetector):
    name = "zero-commit-complete"
    description = "Card moved to Complete but linked branch has 0 commits"
    category = "attribution"

    @staticmethod
    def _norm(name: str) -> str:
        name = name.strip().lstrip("#")
        if name.startswith("http") or name.startswith("pull/"):
            return ""
        return name

    def _init_team_state(self) -> dict:
        return {
            "branch_commits": {},   # normalized branch_name -> commit_count
            "card_branches": {},    # card_id -> normalized branch_name
            "merged_branches": set(),
        }

    async def detect(self, event: Event) -> list[Alert]:
        alerts = []
        s = self.team_state(event.team_id)

        if event.action == "meta.merged_branches":
            for b in event.metadata.get("merged_branches", []):
                s["merged_branches"].add(b)
                s["merged_branches"].add(self._norm(b))
            return alerts

        if event.action == "commit.create" and event.source == "git":
            branch = self._norm(event.metadata.get("branch", ""))
            if branch:
                s["branch_commits"][branch] = s["branch_commits"].get(branch, 0) + 1

        if event.action == "card.tag" and "branch:" in str(event.metadata.get("tag", "")):
            branch = self._norm(str(event.metadata["tag"]).replace("branch:", ""))
            if branch:
                s["card_branches"][event.target] = branch

        if event.action == "card.move":
            to_pipeline = str(event.metadata.get("to_pipeline_name", event.metadata.get("to_pipeline", "")))
            if is_complete_column(to_pipeline):
                card_id = event.target
                branch = s["card_branches"].get(card_id, "")
                if branch and branch not in s["merged_branches"]:
                    commits = s["branch_commits"].get(branch, 0)
                    if commits == 0:
                        alerts.append(self.alert(
                            severity="info",
                            title=f"Card {card_id} completed with 0 commits on branch {branch}",
                            detail=(
                                f"Card {card_id} was moved to Complete by {event.actor}. "
                                f"The linked branch '{branch}' has {commits} known commits."
                            ),
                            team_id=event.team_id,
                            event_ids=[id(event)],
                        ))

        return alerts
