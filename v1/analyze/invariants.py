"""7 invariant checks — detect divergences between independent data sources.

Each invariant is a generic algorithm that takes normalized data structures
and config parameters. No hardcoded team names, member IDs, or file paths.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from ..config import PipelineConfig
from ..normalize.types import Observation


def check_all(commits, branches, cards, files, board_events, config, raw_artifacts=None) -> list[Observation]:
    """Run all 7 invariant checks. Returns list of Observations.

    raw_artifacts: dict with file_history, file_duplicates, branch_state, card_data, board_activities
    """
    raw = raw_artifacts or {}
    file_duplicates = raw.get("file_duplicates", [])
    file_history = raw.get("file_history", {})
    branch_state = raw.get("branch_state", {"branches": {}, "deleted_count": 0})
    card_data = raw.get("card_data", {})
    board_activities = raw.get("board_activities", [])

    members = sorted(set(config.identity_map.values()))

    all_violations = []
    all_violations.extend(_inv1_attribution(file_duplicates, config))
    all_violations.extend(_inv2_branch_deletion(branch_state, card_data))
    all_violations.extend(_inv3_commit_card(card_data, branch_state))
    all_violations.extend(_inv4_branch_integrity(card_data, branch_state))
    all_violations.extend(_inv5_exclusive_authorship(file_history, members))
    all_violations.extend(_inv6_dependency_unlinking(board_activities, config))
    all_violations.extend(_inv7_moved_to_complete(card_data, config.pm_name))

    # Convert to Observation dataclasses
    observations = []
    for v in all_violations:
        inv = v.get("invariant", "")
        # Determine evidence quality
        if inv in ("attribution-preservation", "exclusive-file-authorship"):
            quality = "git-verifiable"
        elif inv in ("branch-deletion-transparency",) and v.get("classification") == "no-digital-record":
            quality = "verbal-possible"
        else:
            quality = "board-verifiable"

        observations.append(Observation(
            invariant=inv,
            evidence_quality=quality,
            description=v.get("note", ""),
            raw=v,
        ))

    # Store raw violations for scoring phase
    check_all._raw_violations = all_violations

    return observations


def _inv1_attribution(file_duplicates: list[dict], config: PipelineConfig) -> list[dict]:
    """Files deleted and re-added under different author with identical content."""
    violations = []
    for d in file_duplicates:
        if not d.get("content_match") or d["original_author"] == d["duplicate_author"]:
            continue
        if any(d["filepath"].startswith(vp) for vp in config.vendor_paths):
            continue
        sanctioned = d["filepath"] in config.sanctioned_transfers
        violations.append({
            "invariant": "attribution-preservation",
            "filepath": d["filepath"],
            "original_author": d["original_author"],
            "original_date": d["original_date"],
            "original_commit": d.get("original_commit", ""),
            "duplicate_author": d["duplicate_author"],
            "duplicate_date": d["duplicate_date"],
            "duplicate_commit": d.get("duplicate_commit", ""),
            "sanctioned_transfer": sanctioned,
            "note": "Documented transfer — authorship attribution should have been preserved."
                    if sanctioned else
                    "No record of sanctioned transfer.",
            "evidence": f"git blob match: {d.get('original_commit', '')} vs {d.get('duplicate_commit', '')}",
        })
    return violations


def _inv2_branch_deletion(branch_state: dict, card_data: dict) -> list[dict]:
    """Branch deletions should have a recorded digital trail."""
    if branch_state.get("deleted_count", 0) == 0:
        return []

    # Build board delgithub lookup
    board_deletes: dict[str, list] = defaultdict(list)
    for card_num, card in card_data.items():
        for b in card.get("branches", []):
            if b["action"] == "remove" and b.get("branch"):
                board_deletes[b["branch"]].append({
                    "by": b["by"], "date": b["date"], "card": card_num,
                })

    violations = []
    for name, bdata in branch_state.get("branches", {}).items():
        if not bdata.get("deleted"):
            continue

        first_author = bdata.get("first_unique_author", "unknown")
        del_events = board_deletes.get(name, [])

        if del_events:
            deleter = del_events[-1]["by"]
            classification = "self-deleted" if deleter == first_author else "deleted-by-other"
        else:
            classification = "no-digital-record"

        merged = bdata.get("unique_commit_count", 0) == 0

        violations.append({
            "invariant": "branch-deletion-transparency",
            "branch": name,
            "first_author": first_author,
            "unique_commits": bdata.get("unique_commit_count", 0),
            "classification": classification,
            "merged_before_deletion": merged,
            "board_delgithub_events": del_events,
            "note": "No digital record of this deletion in board activity."
                    if classification == "no-digital-record" else "",
        })
    return violations


def _inv3_commit_card(card_data: dict, branch_state: dict) -> list[dict]:
    """Card's linked branch should contain commits by the assigned member."""
    violations = []
    for card_num, card in card_data.items():
        assigned = set()
        for m in card.get("members", []):
            if m["action"] == "add":
                assigned.add(m["name"])
            elif m["action"] == "remove":
                assigned.discard(m["name"])
        if not assigned:
            continue

        for b in card.get("branches", []):
            if b["action"] != "add" or not b.get("branch"):
                continue
            branch_name = b["branch"]
            if branch_name.startswith("PR#") or branch_name.startswith("http"):
                continue
            bdata = branch_state.get("branches", {}).get(branch_name)
            if not bdata or not bdata.get("unique_commits"):
                continue
            commit_authors = set(c.get("canonical", c.get("author", "")) for c in bdata["unique_commits"])
            if not commit_authors.intersection(assigned):
                violations.append({
                    "invariant": "commit-card-alignment",
                    "card": card_num,
                    "branch": branch_name,
                    "assigned_members": sorted(assigned),
                    "commit_authors": sorted(commit_authors),
                    "linked_by": b["by"],
                    "linked_date": b["date"],
                })
    return violations


def _inv4_branch_integrity(card_data: dict, branch_state: dict) -> list[dict]:
    """Branches linked to cards should have at least one unique commit."""
    violations = []
    seen = set()
    for card_num, card in card_data.items():
        for b in card.get("branches", []):
            if b["action"] != "add" or not b.get("branch"):
                continue
            branch_name = b["branch"]
            if branch_name.startswith("PR#") or branch_name.startswith("http") or branch_name in seen:
                continue
            seen.add(branch_name)
            bdata = branch_state.get("branches", {}).get(branch_name)
            if bdata and bdata.get("unique_commit_count", 0) == 0:
                violations.append({
                    "invariant": "branch-commit-integrity",
                    "branch": branch_name,
                    "card": card_num,
                    "linked_by": b["by"],
                    "linked_date": b["date"],
                })
    return violations


def _inv5_exclusive_authorship(file_history: dict, members: list[str]) -> list[dict]:
    """Flag first foreign modification of exclusively-authored files."""
    flags = []
    for filepath, events in file_history.items():
        adds = [e for e in events if e["action"] == "A"]
        if not adds:
            continue
        original_author = adds[0]["canonical"]
        if original_author not in members:
            continue

        for e in events:
            if e["action"] == "M" and e["canonical"] != original_author and e["canonical"] in members:
                prior_mods = [p for p in events
                              if p["datetime"] < e["datetime"]
                              and p["canonical"] != original_author
                              and p["action"] in ("A", "M")]
                if not prior_mods:
                    flags.append({
                        "invariant": "exclusive-file-authorship",
                        "filepath": filepath,
                        "original_author": original_author,
                        "modifier": e["canonical"],
                        "commit": e["short"],
                        "date": e["date"],
                        "message": e["message"][:80],
                    })
    return flags


def _inv6_dependency_unlinking(board_activities: list, config: PipelineConfig) -> list[dict]:
    """Flag card dependency unlinks by someone other than the linker."""
    team_uids = set(config.board_user_map.keys())

    link_events: dict[int, list] = defaultdict(list)
    for a in board_activities:
        if a.get("activity_type") in ("linked", "unlinked") and a.get("card_number"):
            who = config.board_user_map.get(a["user_id"], a.get("username", ""))
            link_events[a["card_number"]].append({
                "action": a["activity_type"],
                "by": who,
                "uid": a["user_id"],
                "date": a.get("create_date", ""),
                "detail": (a.get("activity_detail") or "")[:120],
            })

    violations = []
    for card_num, events in link_events.items():
        events.sort(key=lambda e: e["date"])
        last_linker = None
        for e in events:
            if e["action"] == "linked":
                last_linker = e["by"]
            elif e["action"] == "unlinked" and last_linker and e["by"] != last_linker:
                if e["uid"] in team_uids:
                    violations.append({
                        "invariant": "dependency-unlinking",
                        "card": card_num,
                        "linked_by": last_linker,
                        "unlinked_by": e["by"],
                        "unlinked_date": e["date"],
                        "detail": e["detail"],
                    })
    return violations


def _inv7_moved_to_complete(card_data: dict, pm_name: str) -> list[dict]:
    """Flag cards moved to Complete/Closed by someone not assigned."""
    violations = []
    for card_num, card in card_data.items():
        assigned = set()
        for m in card.get("members", []):
            if m["action"] == "add":
                assigned.add(m["name"])
            elif m["action"] == "remove":
                assigned.discard(m["name"])

        for move in card.get("moves", []):
            pipeline = move["pipeline"].strip().lower()
            if pipeline in ("complete", "closed"):
                mover = move["by"]
                if mover not in assigned and assigned:
                    violations.append({
                        "invariant": "completion-attribution",
                        "card": card_num,
                        "pipeline": move["pipeline"],
                        "moved_by": mover,
                        "assigned_to": sorted(assigned),
                        "date": move["date"],
                        "mover_is_pm": mover == pm_name,
                    })
    return violations
