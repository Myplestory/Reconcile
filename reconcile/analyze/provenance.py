"""Branch provenance — origin resolution, fork-point detection, absorption analysis.

Author resolution uses triangulation across 3 independent sources:
  1. Git ref (first_unique_author) — authoritative when available
  2. Board addgithub (first linker) — proves linking, not creation
  3. Commit history (oldest unique commit) — heuristic, not scored

If sources disagree, the conflict itself is the finding.
"""

from __future__ import annotations

import os
import subprocess
from collections import Counter

from ..config import PipelineConfig
from ..normalize.types import Branch, Event


def _get_branch_tip(name: str, *sources: str) -> str | None:
    """Resolve branch ref to tip commit hash."""
    for src in sources:
        if not src or not os.path.isdir(os.path.join(src, ".git")):
            continue
        for ref in [f"origin/{name}", f"origin/#{name}"]:
            try:
                r = subprocess.run(
                    ["git", "-C", src, "log", "-1", "--format=%H", ref],
                    capture_output=True, text=True, check=True,
                )
                tip = r.stdout.strip()
                if tip:
                    return tip
            except subprocess.CalledProcessError:
                continue
    return None


# ── Triangulation ──

def _build_board_creators(board_activities: list, config: PipelineConfig) -> dict[str, str]:
    """Build branch → first linker map from board addgithub events."""
    creators: dict[str, str] = {}
    for a in sorted(board_activities, key=lambda x: x.get("create_date", "")):
        if a.get("activity_type") != "addgithub":
            continue
        detail = a.get("activity_detail", "") or ""
        if not detail.startswith("branch:"):
            continue
        bname = detail[7:]
        if "github.com" in bname:
            if "/tree/" in bname:
                bname = bname.split("/tree/")[-1].lstrip("#")
            else:
                continue
        else:
            bname = bname.lstrip("#")
        if bname not in creators:
            who = config.board_user_map.get(a.get("user_id"), a.get("name", ""))
            creators[bname] = who
    return creators


def resolve_branch_author(branch_name: str, bdata: dict, board_creators: dict, dag: dict) -> tuple[str | None, str, str, dict]:
    """Triangulate branch authorship from 3 independent sources.

    Returns (resolved_author, resolution_method, evidence_quality, signals).

    Resolution methods:
      corroborated        — 2+ sources agree
      majority            — 2 agree, 1 disagrees (conflict flagged)
      single-source       — only 1 source available
      conflict            — all 3 disagree
      unresolvable-clean  — no sources, no commits in DAG
      unresolvable-suspicious — no sources, but commits exist in DAG
    """
    signals: dict[str, str] = {}

    # Signal 1: Git ref
    git_author = bdata.get("first_unique_author")
    if git_author and git_author != "unknown":
        signals["git_ref"] = git_author

    # Signal 2: Board first linker
    clean = branch_name.lstrip("#")
    board_linker = board_creators.get(clean) or board_creators.get(branch_name)
    if board_linker:
        signals["board_linker"] = board_linker

    # Signal 3: Oldest unique commit author
    commits = bdata.get("unique_commits", [])
    if commits:
        oldest = commits[-1]
        author = oldest.get("canonical", oldest.get("author"))
        if author:
            signals["commit_author"] = author

    # Triangulate
    values = list(signals.values())
    unique = set(values)

    if len(values) == 0:
        # Check if branch had activity in the DAG (commits exist as children of tip)
        tip = bdata.get("tip")
        has_dag_children = bool(dag.get("parent_to_children", {}).get(tip)) if tip else False
        if has_dag_children:
            return None, "unresolvable-suspicious", "unresolvable", signals
        return None, "unresolvable-clean", "unresolvable", signals

    if len(unique) == 1:
        method = "corroborated" if len(values) >= 2 else "single-source"
        if "git_ref" in signals:
            quality = "git-verifiable"
        elif "board_linker" in signals:
            quality = "board-verifiable"
        else:
            quality = "heuristic"
        return values[0], method, quality, signals

    # Disagreement — majority wins
    counts = Counter(values)
    winner, win_count = counts.most_common(1)[0]
    if win_count >= 2:
        return winner, "majority", "disputed", signals

    # All disagree
    return None, "conflict", "disputed", signals


# ── Public API ──

def compute(dag: dict, branches: list[Branch], board_events: list[Event], config: PipelineConfig = None) -> dict:
    """Compute branch provenance and parent ancestry with triangulated author resolution.

    Returns dict with:
        provenance: {branch_name: {...}}
        ancestry: [{deleted_branch, resolved_author, resolution_method, ...}]
    """
    if not config:
        return {}

    raw_artifacts = getattr(compute, '_raw_artifacts', {})
    branch_state = raw_artifacts.get("branch_state", {})
    board_activities = raw_artifacts.get("board_activities", [])

    board_creators = _build_board_creators(board_activities, config)
    prov = _compute_provenance(branch_state, board_activities, dag, config, board_creators)
    ancestry = _compute_ancestry(branch_state, dag, config, board_creators)

    return {"provenance": prov, "ancestry": ancestry}


def _compute_provenance(branch_state: dict, board_activities: list, dag: dict,
                        config: PipelineConfig, board_creators: dict) -> dict:
    """Per branch: board creator, first committer, fork parent author."""
    provenance = {}
    for name, bdata in branch_state.get("branches", {}).items():
        entry = {
            "branch": name,
            "board_creator": None, "board_creator_date": None, "board_creator_card": None,
            "first_committer": None,
            "fork_parent_author": None, "fork_parent_commit": None,
        }

        bc_name = board_creators.get(name.lstrip("#")) or board_creators.get(name)
        if bc_name:
            entry["board_creator"] = bc_name

        commits = bdata.get("unique_commits", [])
        if commits:
            oldest = commits[-1]
            entry["first_committer"] = oldest.get("canonical", oldest.get("author", ""))

            parents = dag.get("child_to_parents", {}).get(oldest.get("hash", ""), [])
            if parents:
                parent_info = dag.get("info", {}).get(parents[0])
                if parent_info:
                    entry["fork_parent_author"] = parent_info["author"]
                    entry["fork_parent_commit"] = parents[0][:7]

        provenance[name] = entry
    return provenance


def _compute_ancestry(branch_state: dict, dag: dict, config: PipelineConfig,
                      board_creators: dict) -> list[dict]:
    """For each deleted branch, triangulate author and detect cross-author absorption."""
    members = sorted(set(config.identity_map.values()))
    repo = config.sources.git.path
    fallback = config.sources.git.fallback

    findings = []
    for name, bdata in branch_state.get("branches", {}).items():
        if not bdata.get("deleted"):
            continue

        tip = _get_branch_tip(name, fallback, repo)
        if not tip:
            continue

        if bdata.get("unique_commit_count", 0) == 0:
            tip_info = dag.get("info", {}).get(tip)
            if not tip_info:
                continue

        # Triangulate author
        bdata_with_tip = {**bdata, "tip": tip}
        resolved_author, method, quality, signals = resolve_branch_author(
            name, bdata_with_tip, board_creators, dag
        )

        children = dag.get("parent_to_children", {}).get(tip, [])

        for child_hash in children:
            child_info = dag.get("info", {}).get(child_hash)
            if not child_info:
                continue
            child_author = child_info["author"]

            # Skip self-absorption (normal merge workflow)
            if resolved_author and child_author == resolved_author:
                continue

            # Skip if author unresolvable — can't claim cross-author
            if resolved_author is None and method.startswith("unresolvable"):
                # Still record as evidence gap, but don't score
                if child_author in members:
                    findings.append({
                        "deleted_branch": name,
                        "deleted_author": "unresolved",
                        "resolved_author": None,
                        "resolution_method": method,
                        "evidence_quality": quality,
                        "signals": signals,
                        "deleted_tip": tip[:7],
                        "child_commit": child_hash[:7],
                        "child_full_hash": child_hash,
                        "child_author": child_author,
                        "child_date": child_info.get("date", ""),
                        "child_message": child_info.get("message", "")[:80],
                        "scorable": False,
                    })
                continue

            # Cross-author absorption — scorable finding
            if child_author != resolved_author and child_author in members:
                findings.append({
                    "deleted_branch": name,
                    "deleted_author": bdata.get("first_unique_author", "unknown"),
                    "resolved_author": resolved_author,
                    "resolution_method": method,
                    "evidence_quality": quality,
                    "signals": signals,
                    "deleted_tip": tip[:7],
                    "child_commit": child_hash[:7],
                    "child_full_hash": child_hash,
                    "child_author": child_author,
                    "child_date": child_info.get("date", ""),
                    "child_message": child_info.get("message", "")[:80],
                    "scorable": True,
                })

    return findings
