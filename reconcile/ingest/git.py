"""Git ingest — load commits, branches, and file records from a git repository."""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from datetime import datetime

from ..config import PipelineConfig
from ..normalize.types import Commit, Branch, FileRecord


def _git(repo: str, *args: str) -> str:
    """Run a git command and return stdout."""
    r = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, check=True,
    )
    return r.stdout


def _resolve_repo(config: PipelineConfig) -> str:
    """Return the best available git source path."""
    for path in [config.sources.git.path, config.sources.git.fallback]:
        if path and os.path.isdir(os.path.join(path, ".git")):
            return path
    return config.sources.git.path


def _canonical(author: str, config: PipelineConfig) -> str:
    return config.identity_map.get(author, author)


def load(config: PipelineConfig) -> tuple[list[Commit], list[Branch], list[FileRecord]]:
    """Load git data from repository. Returns (commits, branches, files).

    Also populates config._raw_git with intermediate dicts for downstream
    invariant checks that operate on raw dict structures.
    """
    repo = _resolve_repo(config)
    fallback = config.sources.git.fallback
    has_fallback = fallback and os.path.isdir(os.path.join(fallback, ".git"))

    commits = _load_commits(repo, config)
    branches = _load_branches(repo, fallback if has_fallback else None, config)
    files, file_history = _load_file_history(repo, config)
    duplicates = _find_duplicates(repo, file_history, config)

    # Store raw artifacts as module-level cache for pipeline to pick up
    load._raw = {
        "file_history": file_history,
        "file_duplicates": duplicates,
        "branch_state": _build_branch_state_dict(branches),
    }

    return commits, branches, files


def _load_commits(repo: str, config: PipelineConfig) -> list[Commit]:
    """Load all commits from git log."""
    out = _git(repo, "log", "--all", "--format=%H|%P|%an|%ad|%s", "--date=iso-strict")
    commits = []
    for line in out.strip().split("\n"):
        if not line or "|" not in line:
            continue
        parts = line.split("|", 4)
        sha = parts[0]
        parents = parts[1].split() if parts[1] else []
        author = _canonical(parts[2], config) if len(parts) > 2 else ""
        date_str = parts[3] if len(parts) > 3 else ""
        message = parts[4] if len(parts) > 4 else ""

        try:
            dt = datetime.fromisoformat(date_str)
        except (ValueError, TypeError):
            dt = datetime.min

        commits.append(Commit(
            sha=sha, author=author, date=dt,
            message=message, parents=parents,
        ))
    return commits


def _load_branches(repo: str, fallback: str | None, config: PipelineConfig) -> list[Branch]:
    """Load branch state from one or more git sources."""
    def _list_refs(src: str) -> set[str]:
        out = _git(src, "for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/")
        names = set()
        for line in out.strip().split("\n"):
            name = line.strip().replace("origin/", "")
            if name and name not in ("HEAD", "main", "dev"):
                names.add(name)
        return names

    baseline = _list_refs(fallback) if fallback else set()
    remote = _list_refs(repo)
    if not baseline:
        baseline = remote

    all_names = baseline | remote
    deleted = baseline - remote

    branches = []
    for name in sorted(all_names):
        unique_commits = _unique_commits_for(name, repo, fallback, config)
        first_author = unique_commits[-1].author if unique_commits else "unknown"

        branches.append(Branch(
            name=name, deleted=name in deleted,
            unique_commits=unique_commits, first_author=first_author,
            on_remote=name in remote,
        ))
    return branches


def _unique_commits_for(name: str, repo: str, fallback: str | None, config: PipelineConfig) -> list[Commit]:
    """Get commits unique to a branch (not on main/dev)."""
    sources = [(fallback, "origin/"), (repo, "origin/")] if fallback else [(repo, "origin/")]
    for src, pfx in sources:
        if not src:
            continue
        try:
            out = _git(src, "log", f"{pfx}{name}", "--no-merges",
                       "--format=%H|%an|%ad", "--date=short",
                       "--not", f"{pfx}main", f"{pfx}dev")
            result = []
            for line in out.strip().split("\n"):
                if line and "|" in line:
                    p = line.split("|")
                    result.append(Commit(
                        sha=p[0],
                        author=_canonical(p[1], config),
                        date=datetime.strptime(p[2], "%Y-%m-%d") if len(p) > 2 and p[2] else datetime.min,
                        message="",
                    ))
            return result
        except subprocess.CalledProcessError:
            continue
    return []


def _load_file_history(repo: str, config: PipelineConfig) -> tuple[list[FileRecord], dict]:
    """Load per-file attribution timeline. Returns (FileRecords, raw dict)."""
    out = _git(repo, "log", "--all", "--no-merges", "--name-status",
               "--format=COMMIT|%H|%ad|%an|%s", "--date=iso")

    history: dict[str, list[dict]] = defaultdict(list)
    current = None

    for line in out.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("COMMIT|"):
            parts = line.split("|", 4)
            current = {
                "hash": parts[1], "short": parts[1][:7],
                "date": parts[2][:10], "datetime": parts[2],
                "author": parts[3],
                "canonical": _canonical(parts[3], config),
                "message": parts[4] if len(parts) > 4 else "",
            }
        elif current and "\t" in line:
            parts = line.split("\t")
            action = parts[0][0]
            filepath = parts[-1]
            if not any(filepath.startswith(v) for v in config.vendor_paths):
                history[filepath].append({**current, "action": action})

    for fp in history:
        history[fp].sort(key=lambda e: e["datetime"])

    records = []
    for path, events in history.items():
        adds = [e for e in events if e["action"] == "A"]
        if adds:
            orig = adds[0]
            try:
                dt = datetime.fromisoformat(orig["datetime"].replace(" ", "T").rstrip("T").split("+")[0])
            except (ValueError, TypeError):
                dt = datetime.min
            records.append(FileRecord(
                path=path,
                original_author=orig["canonical"],
                original_date=dt,
                original_commit=orig["hash"],
            ))

    return records, dict(history)


def _find_duplicates(repo: str, file_history: dict, config: PipelineConfig) -> list[dict]:
    """Find files added by different authors with identical content (git blob match)."""
    candidates = []
    for filepath, events in file_history.items():
        adds = [e for e in events if e["action"] == "A"]
        if len(adds) < 2:
            continue
        authors = set(e["canonical"] for e in adds)
        if len(authors) < 2:
            continue
        for i, a1 in enumerate(adds):
            for a2 in adds[i + 1:]:
                if a1["canonical"] != a2["canonical"]:
                    candidates.append((filepath, a1, a2))

    duplicates = []
    for filepath, orig, dupe in candidates:
        try:
            b1 = _git(repo, "rev-parse", f"{orig['hash']}:{filepath}").strip()
            b2 = _git(repo, "rev-parse", f"{dupe['hash']}:{filepath}").strip()
            match = b1 == b2
        except subprocess.CalledProcessError:
            match = False

        duplicates.append({
            "filepath": filepath,
            "original_author": orig["canonical"],
            "original_date": orig["date"],
            "original_commit": orig["short"],
            "duplicate_author": dupe["canonical"],
            "duplicate_date": dupe["date"],
            "duplicate_commit": dupe["short"],
            "content_match": match,
        })
    return duplicates


def _build_branch_state_dict(branches: list[Branch]) -> dict:
    """Convert Branch list to raw dict format for invariant checks."""
    data = {}
    for b in branches:
        data[b.name] = {
            "first_unique_author": b.first_author,
            "unique_commit_count": len(b.unique_commits),
            "unique_commits": [
                {"hash": c.sha, "short": c.sha[:7], "canonical": c.author, "date": c.date.strftime("%Y-%m-%d")}
                for c in b.unique_commits
            ],
            "deleted": b.deleted,
            "on_remote": b.on_remote,
        }
    return {
        "branches": data,
        "deleted_count": sum(1 for b in branches if b.deleted),
    }
