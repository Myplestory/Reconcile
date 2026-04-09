"""Branch resolution classifier — determines why unmerged branches exist.

Classifies each unmerged branch into a resolution state by cross-referencing
git branch data against board activity (card movements, assignments) and
code similarity (rewrite detection).

Academic foundations:
    Coordination:     Cataldo, Herbsleb et al. (2006), "Identification of
                      Coordination Requirements", CSCW — branch abandonment as
                      coordination breakdown signal.
    Ownership:        Fritz et al. (2010), "Degree-of-Authorship Model", ICSE —
                      authorship transfer via code replacement.
    Rework:           Shull, Basili et al. (2002), "What We Have Learned About
                      Fighting Defects", IEEE Metrics — rework cycles as process signal.
    Clone detection:  Roy, Cordy & Koschke (2009), "Clone Detection Techniques",
                      SCP — token similarity for attribution comparison.

Resolution states:
    DEFERRED        — Card explicitly moved to Backlog/Planned from active column.
                      Work paused, not abandoned. (Cataldo: planned coordination)
    REASSIGNED      — Card reassigned to different member while still in progress.
                      Benign handoff. Only valid if card NOT completed.
    REPLACED        — Card completed by different member after original author's
                      branch was started. Original work rejected/overridden.
                      (Shull: unplanned rework)
    UNATTRIBUTED    — Branch author's code appears in merged commit by different
                      author (detected via token containment >= 0.4).
                      Ownership transferred without attribution. (Fritz: ownership transfer)
    SUPERSEDED      — Another branch for the same card was merged instead.
                      Parallel work, one version won. Not malicious.
    BLOCKED         — Card depends on another card that was never completed.
    DUPLICATE_PTR   — Branch points to same commit as other branches (mass branch
                      creation with no unique work). No unique code on branch.
    ABANDONED       — None of the above. Card closed/completed without this branch,
                      no evidence of code reuse. Author stopped working.
    ACTIVE          — Card still in progress, branch has recent commits. Not stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass
class BranchResolution:
    """Resolution for one unmerged branch."""
    branch: str
    author: str
    card_number: str  # extracted from branch name
    commit_sha: str
    commit_date: str
    resolution: str = "unclassified"
    evidence: list[str] = field(default_factory=list)
    related_branches: list[str] = field(default_factory=list)
    severity: str = "info"  # info, elevated, critical


def extract_card_number(branch_name: str) -> str:
    """Extract card/issue number from branch name. Returns '' if none found."""
    import re
    # Match #NNN or NNN_ at start patterns
    m = re.search(r'#?(\d+)', branch_name)
    return m.group(1) if m else ""


def classify_branches(
    unmerged_branches: list[dict],
    board_events: list[dict],
    merged_commits: set[str],
    all_branches: list[dict],
    rewrite_results: list[dict] | None = None,
    pm_member: str | None = None,
) -> list[BranchResolution]:
    """Classify all unmerged branches.

    Args:
        unmerged_branches: [{name, author, sha, date, unique_commits}]
        board_events: [{action, actor, target(card), timestamp, metadata}]
        merged_commits: set of SHAs on main branch
        all_branches: [{name, sha}] — all branches for phantom detection
        pm_member: canonical PM name — PM card.move to Complete is management
                   action, not code replacement. Distinguish from dev completion.
        rewrite_results: [{file, original_author, rewriter, verdict, ...}]

    Returns list of BranchResolution.
    """
    # Pre-compute board state per card
    card_state = _build_card_state(board_events)

    # Detect phantom branches (multiple branches → same SHA)
    sha_to_branches: dict[str, list[str]] = {}
    for b in all_branches:
        sha_to_branches.setdefault(b["sha"], []).append(b["name"])

    # Detect branches whose code was stolen (appears in other author's merged commits)
    transferred_files: dict[str, list[dict]] = {}
    if rewrite_results:
        for rw in rewrite_results:
            if rw.get("verdict") in ("cosmetic-rewrite", "partial-derivation"):
                orig = rw.get("original_author", "")
                transferred_files.setdefault(orig, []).append(rw)

    resolutions: list[BranchResolution] = []

    for branch in unmerged_branches:
        name = branch["name"]
        author = branch["author"]
        sha = branch["sha"]
        card = extract_card_number(name)

        resolution = BranchResolution(
            branch=name, author=author, card_number=card,
            commit_sha=sha, commit_date=branch.get("date", ""),
        )

        # --- PHANTOM: same SHA as other branches ---
        siblings = sha_to_branches.get(sha, [])
        if len(siblings) > 1:
            others = [s for s in siblings if s != name]
            resolution.resolution = "duplicate-pointer"
            resolution.related_branches = others
            resolution.evidence.append(
                f"Branch points to same commit as {len(others)} other branch(es): "
                f"{', '.join(others[:5])}. No unique work on this branch."
            )
            resolution.severity = "elevated"
            resolutions.append(resolution)
            continue

        # --- Card-based resolution (if card number found) ---
        if card and card in card_state:
            cs = card_state[card]

            # DEFERRED: card moved back to Backlog/Planned
            if cs["current_pipeline"] in ("backlog", "planned") and cs["regression_count"] > 0:
                resolution.resolution = "deferred"
                resolution.evidence.append(
                    f"Card #{card} currently in {cs['current_pipeline']}. "
                    f"Regressed {cs['regression_count']} time(s)."
                )
                resolutions.append(resolution)
                continue

            # Check completion state
            completed_by = cs.get("completed_by", "")
            is_completed = cs["current_pipeline"] in ("complete", "closed", "done")

            # Card completed by different actor than branch author
            if is_completed and completed_by and completed_by != author:
                # PM-CLOSE: PM moved card to Complete as management action.
                # This is normal PM workflow, not code replacement.
                # Per process: PM reviews and closes cards. The code author
                # is whoever made the last code-related action on this card.
                if pm_member and completed_by == pm_member:
                    # Find last code-related actor on this card
                    last_code_actor = _find_last_code_actor(card, board_events)
                    if last_code_actor == author:
                        # Branch author IS the last code actor — PM just closed it
                        resolution.resolution = "pm-closed"
                        resolution.evidence.append(
                            f"Card #{card} closed by PM ({pm_member}). "
                            f"Last code-related activity by branch author {author}. "
                            f"Normal PM workflow — branch may not have been merged via git."
                        )
                    else:
                        resolution.resolution = "pm-closed"
                        resolution.severity = "info"
                        actor_note = f" Last code activity by {last_code_actor}." if last_code_actor else ""
                        resolution.evidence.append(
                            f"Card #{card} closed by PM ({pm_member}).{actor_note} "
                            f"Branch author: {author}."
                        )
                    resolutions.append(resolution)
                    continue

                # REPLACED: non-PM completed the card, branch author's work overridden
                if cs["regression_count"] > 0:
                    resolution.resolution = "replaced"
                    resolution.severity = "elevated"
                    resolution.evidence.append(
                        f"Card #{card} completed by {completed_by}, not branch author {author}. "
                        f"Card had {cs['regression_count']} regression(s) before final completion. "
                        f"Original author's work was overridden."
                    )
                else:
                    resolution.resolution = "replaced"
                    resolution.severity = "info"
                    resolution.evidence.append(
                        f"Card #{card} completed by {completed_by}, not branch author {author}."
                    )
                resolutions.append(resolution)
                continue

            # REASSIGNED: card assigned to different member, still in progress
            if cs["current_assignee"] and cs["current_assignee"] != author and not is_completed:
                resolution.resolution = "reassigned"
                resolution.evidence.append(
                    f"Card #{card} reassigned to {cs['current_assignee']}. "
                    f"Still in {cs['current_pipeline']}."
                )
                resolutions.append(resolution)
                continue

        # --- STOLEN: author's code appears in someone else's merged commits ---
        if author in transferred_files:
            relevant = [rw for rw in transferred_files[author]
                        if rw.get("containment", 0) >= 0.4]
            if relevant:
                resolution.resolution = "unattributed-transfer"
                resolution.severity = "elevated"
                files = [rw["file"] for rw in relevant[:3]]
                resolution.evidence.append(
                    f"Code attributed to {author} found in merged commits by others. "
                    f"Files: {', '.join(files)}. Token containment >= 40%."
                )
                resolutions.append(resolution)
                continue

        # --- SUPERSEDED: another branch for same card exists and was merged ---
        if card:
            same_card_merged = False
            for b in all_branches:
                if b["name"] == name:
                    continue
                other_card = extract_card_number(b["name"])
                if other_card == card and b["sha"] in merged_commits:
                    same_card_merged = True
                    resolution.related_branches.append(b["name"])
            if same_card_merged:
                resolution.resolution = "superseded"
                resolution.evidence.append(
                    f"Another branch for card #{card} was merged: "
                    f"{', '.join(resolution.related_branches[:3])}."
                )
                resolutions.append(resolution)
                continue

        # --- ABANDONED: default ---
        resolution.resolution = "abandoned"
        resolution.evidence.append(
            f"No deferred, reassigned, replaced, stolen, or superseded evidence found. "
            f"Branch author: {author}."
        )
        resolutions.append(resolution)

    return resolutions


def _find_last_code_actor(card: str, board_events: list[dict]) -> str:
    """Find the last actor who performed a code-related action on a card.

    Code-related: card.tag (github link), card.move to Testing/Complete.
    NOT code-related: card.assign, card.create (PM actions).
    """
    code_actions = {"card.tag", "card.link"}
    code_pipelines = {"testing", "in progress"}
    last_actor = ""
    for e in sorted(board_events, key=lambda x: x.get("timestamp", "")):
        if str(e.get("target", "")) != card:
            continue
        action = e.get("action", "")
        if action in code_actions:
            last_actor = e.get("actor", "")
        elif action == "card.move":
            to_name = (e.get("metadata", {}).get("to_pipeline_name", "") or "").lower()
            if to_name in code_pipelines:
                last_actor = e.get("actor", "")
    return last_actor


def _build_card_state(board_events: list[dict]) -> dict[str, dict]:
    """Build per-card state from board activity timeline.

    Returns {card_number: {current_pipeline, completed_by, current_assignee,
                           regression_count, move_history}}.
    """
    cards: dict[str, dict] = {}

    for e in sorted(board_events, key=lambda x: x.get("timestamp", "")):
        target = str(e.get("target", ""))
        if not target:
            continue

        if target not in cards:
            cards[target] = {
                "current_pipeline": "backlog",
                "completed_by": "",
                "current_assignee": "",
                "regression_count": 0,
                "move_history": [],
            }

        cs = cards[target]
        action = e.get("action", "")
        actor = e.get("actor", "")
        meta = e.get("metadata", {})

        if action == "card.move":
            to_name = (meta.get("to_pipeline_name", "") or "").lower()
            prev = cs["current_pipeline"]
            cs["move_history"].append((prev, to_name, actor))

            # Track regression: moving backward from active to backlog
            if to_name == "backlog" and prev not in ("backlog", "planned", ""):
                cs["regression_count"] += 1

            # Track completion
            if to_name in ("complete", "done", "closed"):
                cs["completed_by"] = actor

            cs["current_pipeline"] = to_name

        elif action == "card.assign":
            member_id = meta.get("member_id", actor)
            cs["current_assignee"] = member_id

    return cards


def summarize_resolutions(resolutions: list[BranchResolution]) -> dict[str, Any]:
    """Aggregate resolution counts by type and author."""
    by_type: dict[str, int] = {}
    by_author: dict[str, dict[str, int]] = {}

    for r in resolutions:
        by_type[r.resolution] = by_type.get(r.resolution, 0) + 1
        if r.author not in by_author:
            by_author[r.author] = {}
        by_author[r.author][r.resolution] = by_author[r.author].get(r.resolution, 0) + 1

    return {
        "total": len(resolutions),
        "by_type": by_type,
        "by_author": by_author,
        "resolutions": [
            {
                "branch": r.branch, "author": r.author, "card": r.card_number,
                "resolution": r.resolution, "severity": r.severity,
                "evidence": r.evidence, "related": r.related_branches,
            }
            for r in resolutions
        ],
    }
