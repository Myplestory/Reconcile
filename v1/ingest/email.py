"""Email archive ingest — parse .eml status report files."""

from __future__ import annotations

import email as email_lib
import glob
import os
import re
from datetime import datetime

from ..config import PipelineConfig
from ..normalize.types import Report, MemberMarking


def parse_eml(path: str) -> dict:
    """Parse a single .eml file. Returns raw dict with date, submission, linkid, members."""
    with open(path, "r") as f:
        msg = email_lib.message_from_file(f)

    basename = os.path.basename(path)
    parts = basename.replace("status-report_", "").replace(".eml", "").split("_")
    report_date = parts[0]
    sub_num = parts[1] if len(parts) > 1 else "unknown"

    smtp_date = msg["Date"] or ""

    # Decode HTML body
    html = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                html = payload.decode("utf-8", errors="replace")
            break

    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()

    linkid_match = re.search(r"linkid=(\d+)", html)
    linkid = int(linkid_match.group(1)) if linkid_match else None

    members = {}
    entries = re.findall(
        r"Name\s*:\s*([\w\s-]+?)\s+Task Assigned.*?Marked as\s*:\s*"
        r"([A-Za-z ]+?)\[(\d+)/(\d+)/(\d+)\]\[(\d+)\]",
        text,
    )
    for name, status, on_time, late, absent, total in entries:
        members[name.strip()] = {
            "status": status.strip(),
            "cumulative": [int(on_time), int(late), int(absent)],
            "cumulative_total": int(total),
        }

    return {
        "date": report_date,
        "submission": sub_num,
        "smtp_date": smtp_date,
        "linkid": linkid,
        "members": members,
        "source_file": basename,
    }


def _parse_date(date_str: str) -> datetime:
    """Parse a date string from a report."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        return datetime.min


def _classify_status(status_str: str) -> tuple[str, str]:
    """Classify status into (attendance, preparedness)."""
    s = status_str.lower()
    if "absent" in s:
        attendance = "Absent"
    elif "late" in s:
        attendance = "Late"
    else:
        attendance = "On Time"
    prepared = "Unprepared" if "unprepared" in s else "Prepared"
    return attendance, prepared


def load(config: PipelineConfig) -> list[Report]:
    """Load all .eml status reports from the email directory."""
    email_dir = config.sources.email_dir
    if not email_dir or not os.path.isdir(email_dir):
        return []

    eml_files = sorted(glob.glob(os.path.join(email_dir, "status-report_*.eml")))
    if not eml_files:
        return []

    reports = []
    for path in eml_files:
        raw = parse_eml(path)

        members = {}
        for name, data in raw["members"].items():
            attendance, prepared = _classify_status(data["status"])
            cum = data.get("cumulative", [0, 0, 0])
            members[name] = MemberMarking(
                name=name,
                attendance=attendance,
                prepared=prepared,
                ontime_cumulative=cum[0] if len(cum) > 0 else 0,
                late_cumulative=cum[1] if len(cum) > 1 else 0,
                absent_cumulative=cum[2] if len(cum) > 2 else 0,
                score=data.get("cumulative_total", 0),
            )

        reports.append(Report(
            linkid=raw["linkid"] or 0,
            date=_parse_date(raw["date"]),
            meeting_date=raw["date"],
            source_file=raw["source_file"],
            members=members,
            raw_headers={"smtp_date": raw["smtp_date"], "submission": raw["submission"]},
        ))

    return reports
