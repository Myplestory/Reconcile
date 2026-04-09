"""Commit DAG construction — forward and inverted parent indices."""

from __future__ import annotations

import re
from collections import defaultdict

from ..config import PipelineConfig
from ..normalize.types import Commit


def build(commits: list[Commit], config: PipelineConfig = None) -> dict:
    """Build DAG from commit list.

    Returns dict with:
        info: {sha: {author, date, message}}
        parent_to_children: {parent_sha: [child_sha, ...]}
        child_to_parents: {child_sha: [parent_sha, ...]}
    """
    info = {}
    parent_to_children: dict[str, list[str]] = defaultdict(list)
    child_to_parents: dict[str, list[str]] = {}

    for c in commits:
        info[c.sha] = {
            "author": c.author,
            "date": c.date.isoformat() if c.date else "",
            "message": c.message,
        }
        child_to_parents[c.sha] = c.parents
        for p in c.parents:
            parent_to_children[p].append(c.sha)

    return {
        "info": info,
        "parent_to_children": dict(parent_to_children),
        "child_to_parents": dict(child_to_parents),
    }


def build_merge_lookup(dag: dict) -> dict:
    """Build branch → merge info from merge commit messages.

    Returns: {branch_name: [{merge_sha, merge_author, pr_number, merge_date, target_branch}]}
    """
    merges: dict[str, list] = defaultdict(list)

    pr_pattern = re.compile(r"Merge pull request #(\d+) from .*/(.+)")
    branch_pattern = re.compile(r"Merge branch '(.+?)' into (.+)")

    for sha, info in dag.get("info", {}).items():
        msg = info.get("message", "")
        parents = dag.get("child_to_parents", {}).get(sha, [])
        if len(parents) < 2:
            continue

        m = pr_pattern.match(msg)
        if m:
            pr_num = int(m.group(1))
            branch = m.group(2)
            merges[branch].append({
                "merge_sha": sha,
                "merge_author": info["author"],
                "pr_number": pr_num,
                "merge_date": info["date"],
                "target_branch": "main",
            })
            continue

        m = branch_pattern.match(msg)
        if m:
            branch = m.group(1)
            target = m.group(2)
            merges[branch].append({
                "merge_sha": sha,
                "merge_author": info["author"],
                "pr_number": None,
                "merge_date": info["date"],
                "target_branch": target,
            })

    return dict(merges)
