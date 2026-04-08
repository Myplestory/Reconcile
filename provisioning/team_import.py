"""Parse team roster files (JSON/CSV) into normalized dicts.

No external dependencies. No Discord knowledge. Pure data transform.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path


def parse_team_import(path: str | Path) -> list[dict]:
    """Parse JSON or CSV into list of team dicts.

    Returns:
        [{"team_id": str, "team_name": str, "members": [{"name": str, "email": str, "role": str}]}]

    JSON format: list of objects matching the return schema.
    CSV format: columns team_id, team_name, member_name, member_email, member_role
                (one row per member; rows with same team_id grouped).
    """
    path = Path(path)
    if path.suffix == ".json":
        return _parse_json(path)
    elif path.suffix == ".csv":
        return _parse_csv(path)
    else:
        raise ValueError(f"Unsupported format: {path.suffix} (expected .json or .csv)")


def _parse_json(path: Path) -> list[dict]:
    with open(path) as f:
        data = json.load(f)
    for team in data:
        if "team_id" not in team:
            raise ValueError(f"Missing team_id in {team}")
        if "members" not in team:
            raise ValueError(f"Missing members in {team}")
    return data


def _parse_csv(path: Path) -> list[dict]:
    teams: dict[str, dict] = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        required = {"team_id", "member_name"}
        if reader.fieldnames:
            missing = required - set(reader.fieldnames)
            if missing:
                raise ValueError(f"CSV missing required columns: {missing}")
        for row in reader:
            tid = row["team_id"]
            if tid not in teams:
                teams[tid] = {
                    "team_id": tid,
                    "team_name": row.get("team_name", tid),
                    "members": [],
                }
            teams[tid]["members"].append({
                "name": row["member_name"],
                "email": row.get("member_email", ""),
                "role": row.get("member_role", "developer"),
            })
    return list(teams.values())
