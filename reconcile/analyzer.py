"""Historical analyzer. Runs the full scoring pipeline on accumulated events.

Three trigger modes:
  1. On-demand:  analyzer.sweep(team_id)
  2. On-anomaly: bus calls analyzer.sweep() when a detector fires
  3. Scheduled:  daily cron calls analyzer.sweep_all()

The analyzer reads the event timeline (immutable), computes profiles,
and writes results. It does not modify the timeline or affect real-time
detection. It can run concurrently with the event bus.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .schema import Event, is_complete_column

log = logging.getLogger(__name__)


@dataclass
class MemberProfile:
    """Accumulated profile for one team member."""

    member: str
    flags: list = field(default_factory=list)
    perpetrator_score: int = 0
    victim_score: int = 0
    direction: str = "neutral"

    # Counters
    cards_completed: int = 0
    cards_completed_zero_commits: int = 0
    branches_deleted: int = 0
    files_reattributed_to: int = 0
    files_reattributed_from: int = 0
    commits: int = 0
    messages_sent: int = 0
    proactive_count: int = 0
    meetings_present: int = 0
    meetings_absent: int = 0


class HistoricalAnalyzer:
    """Two-pass historical analysis on the event timeline.

    Pass 1: Build base profiles from timeline events
    Pass 2: Score PM actions, compute pair escalation, classify direction

    All state is derived from the timeline. No mutation of input data.
    Thread-safe: creates new profile objects on each sweep.
    """

    def __init__(self, config: dict | None = None, pipeline_map: dict[str, str] | None = None,
                 email_dir: str = "statusreports/"):
        self.config = config or {}
        self._pipeline_map = pipeline_map or {}
        # Reverse lookup: pipeline name → pipeline ID(s)
        self._closed_pipeline_ids = {
            pid for pid, name in self._pipeline_map.items()
            if name.lower() == "closed"
        }
        self._email_dir = email_dir
        self._last_sweep: dict[str, datetime] = {}

    async def sweep(self, timeline: list[Event], team_id: str = "default", members: set[str] | None = None) -> dict[str, MemberProfile]:
        """Run full historical analysis on a timeline. Returns member profiles.

        Args:
            members: If provided, only profile these actors. Non-members still
                     participate in detection (e.g., branch authorship) but are
                     excluded from the returned profiles.
        """
        log.info("Historical sweep for team %s (%d events)", team_id, len(timeline))

        profiles = await asyncio.get_event_loop().run_in_executor(
            None, self._compute_profiles, timeline, members
        )

        self._last_sweep[team_id] = datetime.now(timezone.utc)
        return profiles

    @staticmethod
    def _normalize_branch(name: str) -> str:
        """Normalize branch name: strip # prefix, ignore URLs/PRs."""
        name = name.strip().lstrip("#")
        if name.startswith("http") or name.startswith("pull/"):
            return ""
        return name

    def _compute_profiles(self, timeline: list[Event], members: set[str] | None = None) -> dict[str, MemberProfile]:
        """Pass 1: Build profiles from timeline. Pure function, no side effects."""
        profiles: dict[str, MemberProfile] = {}
        card_branch_map: dict[str, str] = {}       # card_id → current branch (last-wins; original tracked in card_state)
        branch_commits: dict[str, int] = defaultdict(int)
        card_owners: dict[str, str] = {}            # card_id → first assigned member
        branch_to_card: dict[str, str] = {}         # normalized branch → card_id

        # Per-card attribution deviation tracking
        card_state: dict[str, dict] = {}
        # card_id → {owner, original_branch, current_branch, deviations[], closed_by, deleted}

        def _get_card(card_id: str) -> dict:
            if card_id not in card_state:
                card_state[card_id] = {
                    "owner": "", "original_branch": "", "current_branch": "",
                    "deviations": [], "closed_by": None, "deleted": False,
                    "has_unlinked": False,
                }
            return card_state[card_id]

        # Track which canonical members committed on each branch
        branch_commit_authors: dict[str, set[str]] = defaultdict(set)

        # Pre-scan: collect merged branches + git branch existence
        merged_branches: set[str] = set()
        git_branches_exist: set[str] = set()
        for event in timeline:
            if event.action == "meta.merged_branches":
                for b in event.metadata.get("merged_branches", []):
                    merged_branches.add(b)
                    n = self._normalize_branch(b)
                    merged_branches.add(n)
                    git_branches_exist.add(b)
                    git_branches_exist.add(n)

        for event in timeline:
            if event.action in ("meta.merged_branches", "meta.branch_authors"):
                continue

            actor = event.actor
            if actor not in profiles:
                profiles[actor] = MemberProfile(member=actor)
            p = profiles[actor]

            # --- Track state ---
            if event.action == "commit.create":
                p.commits += 1
                branch = self._normalize_branch(event.metadata.get("branch", ""))
                if branch:
                    branch_commits[branch] += 1
                    branch_commit_authors[branch].add(actor)  # canonical name (resolved by inject endpoint)

            if event.action == "card.assign":
                assigned = event.metadata.get("assigned_member", "")
                if assigned and event.target not in card_owners:
                    card_owners[event.target] = assigned
                    _get_card(event.target)["owner"] = assigned

            if event.action == "card.tag" and "branch:" in str(event.metadata.get("tag", "")):
                branch = self._normalize_branch(
                    str(event.metadata["tag"]).replace("branch:", "")
                )
                if branch:
                    card_id = event.target
                    card_branch_map[card_id] = branch
                    branch_to_card[branch] = card_id
                    cs = _get_card(card_id)
                    if not cs["original_branch"]:
                        cs["original_branch"] = branch
                    # If branch changed after an unlink, record replacement
                    if cs["has_unlinked"] and branch != cs["current_branch"] and cs["current_branch"]:
                        cs["deviations"].append({
                            "type": "branch.replaced", "actor": actor,
                            "date": event.timestamp.isoformat()[:10],
                            "detail": f"{cs['current_branch']} → {branch}",
                        })
                        owner = cs["owner"]
                        if owner and actor != owner:
                            cs["deviations"].append({
                                "type": "cross-person", "actor": actor,
                                "date": event.timestamp.isoformat()[:10],
                                "detail": f"Branch replaced by {actor} (owner: {owner})",
                            })
                    cs["current_branch"] = branch
                    cs["has_unlinked"] = False

            if event.action == "message.send":
                p.messages_sent += 1
                if event.metadata.get("proactive"):
                    p.proactive_count += 1

            # --- Detect deviations ---

            if event.action == "branch.delete":
                branch = self._normalize_branch(event.target)
                card_number = event.metadata.get("card_number", "")
                card_id = branch_to_card.get(branch, card_number)
                if card_id:
                    cs = _get_card(card_id)
                    cs["has_unlinked"] = True
                    # Who actually committed on this branch?
                    git_authors = branch_commit_authors.get(branch, set())
                    owner = cs["owner"]
                    cs["deviations"].append({
                        "type": "branch.unlinked", "actor": actor,
                        "date": event.timestamp.isoformat()[:10],
                        "detail": f"{branch} removed by {actor} (git authors: {', '.join(sorted(git_authors)) or 'unknown'})",
                    })
                    if owner and actor != owner:
                        cs["deviations"].append({
                            "type": "cross-person", "actor": actor,
                            "date": event.timestamp.isoformat()[:10],
                            "detail": f"Branch unlinked by {actor} (owner: {owner})",
                        })
                    # If the branch had commits by the owner but was deleted by someone else
                    if owner and owner in git_authors and actor != owner:
                        cs["deviations"].append({
                            "type": "owner-work-unlinked", "actor": actor,
                            "date": event.timestamp.isoformat()[:10],
                            "detail": f"{owner} had commits on {branch}, unlinked by {actor}",
                        })

            if event.action == "card.move":
                to_pipeline = str(event.metadata.get("to_pipeline_name", event.metadata.get("to_pipeline", "")))
                card = event.target

                if is_complete_column(to_pipeline):
                    p.cards_completed += 1
                    # Zero-commit: contribution flag. Skip merged branches.
                    branch = card_branch_map.get(card, "")
                    commits = branch_commits.get(branch, 0)
                    if branch and commits == 0 and branch not in merged_branches:
                        p.cards_completed_zero_commits += 1
                        p.flags.append({
                            "type": "zero-commit-completion",
                            "severity": "low",
                            "actor": actor,
                            "date": event.timestamp.isoformat(),
                            "detail": f"Card {card} completed with 0 commits on {branch}",
                        })

                # Closed pipeline = PM locked attribution
                raw_pipe = str(event.metadata.get("to_pipeline", ""))
                if raw_pipe in self._closed_pipeline_ids or "closed" in to_pipeline.lower():
                    cs = _get_card(card)
                    if cs["deviations"]:  # only track if card has prior deviations
                        cs["closed_by"] = actor
                        cs["deviations"].append({
                            "type": "card.closed", "actor": actor,
                            "date": event.timestamp.isoformat()[:10],
                            "detail": f"Closed by {actor}",
                        })

            if event.action == "card.delete":
                card = event.target
                cs = _get_card(card)
                cs["deleted"] = True
                cs["deviations"].append({
                    "type": "card.deleted", "actor": actor,
                    "date": event.timestamp.isoformat()[:10],
                    "detail": f"Card deleted by {actor}",
                })

            if event.action == "file.create":
                path = event.target
                content_hash = event.metadata.get("content_hash", "")
                prev_author = event.metadata.get("original_author", "")
                if prev_author and prev_author != actor and content_hash:
                    p.files_reattributed_to += 1
                    p.flags.append({
                        "type": "file-reattribution",
                        "severity": "high",
                        "actor": actor,
                        "victim": prev_author,
                        "date": event.timestamp.isoformat(),
                        "detail": f"Re-added {path} (originally {prev_author}, byte-identical)",
                    })
                    # Victim flag (ensure profile exists)
                    if prev_author not in profiles:
                        profiles[prev_author] = MemberProfile(member=prev_author)
                    profiles[prev_author].files_reattributed_from += 1
                    profiles[prev_author].flags.append({
                        "type": "file-reattributed-away",
                        "severity": "high",
                        "actor": actor,
                        "victim": prev_author,
                        "date": event.timestamp.isoformat(),
                        "detail": f"{path} re-attributed to {actor}",
                    })

        # --- Pass 2: Per-card deviation escalation → consolidated flags ---
        # Check git branch existence for cards with deviations
        for card_id, cs in card_state.items():
            if not cs["deviations"]:
                continue
            orig = cs["original_branch"]
            if orig and orig not in git_branches_exist:
                cs["deviations"].append({
                    "type": "git.branch.missing", "actor": "system",
                    "date": "", "detail": f"Original branch {orig} no longer exists in git",
                })

        # Generate consolidated flag per card with deviations
        for card_id, cs in card_state.items():
            devs = cs["deviations"]
            if not devs:
                continue
            owner = cs["owner"]

            # Escalation severity
            n = len(devs)
            has_instant = any(d["type"] in ("card.deleted", "git.branch.missing") for d in devs)
            if has_instant:
                severity = "critical"
            elif n >= 3:
                severity = "suspect"
            elif n >= 2:
                severity = "elevated"
            else:
                severity = "info"

            # Build detail string
            chain = "; ".join(
                f"{d['type']}: {d['detail']}" for d in devs
            )
            detail = f"Card {card_id} (owner: {owner or '?'}) — {n} deviation(s): {chain}"

            # Identify perpetrators (actors who aren't the owner)
            perp_actors = set()
            for d in devs:
                if d["actor"] != owner and d["actor"] != "system" and d["type"] != "card.closed":
                    perp_actors.add(d["actor"])

            # Flag on each perpetrator's profile
            for perp in perp_actors:
                if perp not in profiles:
                    profiles[perp] = MemberProfile(member=perp)
                profiles[perp].branches_deleted += 1
                profiles[perp].flags.append({
                    "type": "attribution-deviation",
                    "severity": severity,
                    "actor": perp,
                    "victim": owner,
                    "date": devs[0]["date"],
                    "detail": detail,
                })

            # Victim flag on owner
            if owner and perp_actors:
                if owner not in profiles:
                    profiles[owner] = MemberProfile(member=owner)
                profiles[owner].flags.append({
                    "type": "attribution-victim",
                    "severity": severity,
                    "actor": ", ".join(sorted(perp_actors)),
                    "victim": owner,
                    "date": devs[0]["date"],
                    "detail": detail,
                })

            # If PM closed a card with deviations, flag PM involvement
            if cs["closed_by"] and cs["closed_by"] not in perp_actors and perp_actors:
                pm = cs["closed_by"]
                if pm not in profiles:
                    profiles[pm] = MemberProfile(member=pm)
                profiles[pm].flags.append({
                    "type": "attribution-pm-sanctioned",
                    "severity": severity,
                    "actor": pm,
                    "victim": owner,
                    "date": devs[-1]["date"],
                    "detail": f"PM {pm} closed card {card_id} with {n} prior deviations. {detail}",
                })

        # --- Compute scores and direction ---
        # Severity-weighted: critical=4, suspect=3, elevated=2, info=1
        _SEV_WEIGHT = {"critical": 4, "suspect": 3, "elevated": 2, "info": 1}
        for member, p in profiles.items():
            p.perpetrator_score = sum(
                _SEV_WEIGHT.get(f.get("severity", "info"), 1)
                for f in p.flags if f["type"] in (
                    "attribution-deviation", "file-reattribution",
                )
            )
            p.victim_score = sum(
                _SEV_WEIGHT.get(f.get("severity", "info"), 1)
                for f in p.flags if f["type"] in (
                    "attribution-victim", "file-reattributed-away",
                )
            )

            if p.perpetrator_score > 0 and p.victim_score == 0:
                p.direction = "perpetrator"
            elif p.victim_score > 0 and p.perpetrator_score == 0:
                p.direction = "victim"
            elif p.perpetrator_score > 0 and p.victim_score > 0:
                p.direction = "mixed"
            else:
                p.direction = "neutral"

        # --- Pass 3: PM-authoritative attendance from status reports ---
        from .analyze.attendance import parse_status_reports
        attendance = parse_status_reports(self._email_dir)
        for name, record in attendance.items():
            if name in profiles:
                profiles[name].meetings_present = record["present"]
                profiles[name].meetings_absent = record["absent"]

        # Filter to known members only (if provided)
        if members:
            profiles = {k: v for k, v in profiles.items() if k in members}

        return profiles

    async def sweep_collaboration(
        self,
        timeline: list[Event],
        team_id: str = "default",
        members: set[str] | None = None,
        sprint_start: datetime | None = None,
        sprint_end: datetime | None = None,
        git_churn: dict | None = None,
        pm_member: str | None = None,
        commit_classifier=None,
        git_repo: str = "",
        git_author_map: dict[str, str] | None = None,
    ) -> dict:
        """Compute collaboration metrics for a sprint window.

        Runs in executor (CPU-bound). Returns metric dict suitable for
        storage in collaboration_snapshots table.

        If commit_classifier is provided, commits are classified via NLI +
        deterministic fusion. Classification priority:
          1. Classifier cache (hot — populated by prior replay or sweep)
          2. Full git parse + classify_batch (if git_repo available)
          3. Lightweight event-based classification (message-only, no diffs)
        """
        from .analyze.collaboration import compute_collaboration_metrics

        # Convert Event objects to dicts for the metric module
        event_dicts = []
        for e in timeline:
            d = {
                "timestamp": e.timestamp,
                "source": e.source,
                "team_id": e.team_id,
                "actor": e.actor,
                "action": e.action,
                "target": e.target,
                "target_type": e.target_type,
                "metadata": e.metadata,
            }
            event_dicts.append(d)

        # Classification priority:
        #   1. Classifier cache (hot from prior replay/sweep)
        #   2. Full git parse → classify_batch (diff-aware, most precise)
        #   3. Lightweight classify_from_events (message-only, no diffs)
        classifications = None
        if commit_classifier and hasattr(commit_classifier, '_cache'):
            if commit_classifier._cache:
                classifications = commit_classifier._cache
            else:
                # Try full git parse first (diff-aware classification)
                if git_repo:
                    try:
                        import os
                        import subprocess
                        repo_path = git_repo
                        if os.path.isdir(os.path.join(repo_path, ".git")):
                            from .analyze.code_quality import parse_git_log_patch
                            loop = asyncio.get_running_loop()
                            proc = await loop.run_in_executor(
                                None,
                                lambda: subprocess.run(
                                    ["git", "-C", repo_path, "log", "--all", "--no-merges",
                                     "-p", "--format=COMMIT:%H|%aN|%aI|%s"],
                                    capture_output=True, timeout=60,
                                ),
                            )
                            stdout = proc.stdout.decode("utf-8", errors="replace")
                            commits = parse_git_log_patch(stdout, git_author_map or {})
                            if commits:
                                results = await commit_classifier.classify_batch(commits)
                                classifications = results
                                nli_count = sum(1 for r in results.values() if r.get("source") == "nli")
                                log.info("Classified %d commits via git parse (%d NLI)", len(results), nli_count)
                    except Exception as e:
                        log.warning("Git parse classification failed: %s — falling back to event-based", e)

                # Fallback: classify from event metadata (no git repo needed)
                if not classifications:
                    try:
                        results = await commit_classifier.classify_from_events(event_dicts)
                        if results:
                            classifications = results
                            log.info("Classified %d commits from event metadata (lightweight)", len(results))
                    except Exception as e:
                        log.warning("Lightweight classification failed: %s", e)

        from functools import partial
        compute_fn = partial(
            compute_collaboration_metrics,
            event_dicts,
            members or set(),
            sprint_start,
            sprint_end,
            git_churn,
            self._pipeline_map,
            pm_member,
            commit_classifications=classifications,
        )

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, compute_fn)

    async def sweep_all(self, timelines: dict[str, list[Event]]) -> dict[str, dict[str, MemberProfile]]:
        """Batch sweep across all teams. Run concurrently."""
        results = await asyncio.gather(
            *(self.sweep(tl, team_id) for team_id, tl in timelines.items())
        )
        return dict(zip(timelines.keys(), results))
