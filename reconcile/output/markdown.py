"""Markdown report generation — summary of pipeline findings."""

from __future__ import annotations

import os
from collections import Counter

from ..normalize.types import PipelineState
from ..config import PipelineConfig


def write(state: PipelineState, config: PipelineConfig) -> None:
    """Write markdown reports to output directory."""
    out = config.output_dir
    os.makedirs(out, exist_ok=True)

    _write_violations(state, config, out)
    _write_timeline(state, config, out)


def _write_violations(state: PipelineState, config: PipelineConfig, out: str) -> None:
    """Write invariant-violations.md."""
    path = os.path.join(out, "invariant-violations.md")
    lines = ["# Invariant Violations\n"]

    if not state.observations:
        lines.append("No violations detected.\n")
    else:
        # Group by invariant
        by_inv: dict[str, list] = {}
        for obs in state.observations:
            by_inv.setdefault(obs.invariant, []).append(obs)

        lines.append(f"**Total: {len(state.observations)} observations across {len(by_inv)} invariants**\n")

        for inv, obs_list in sorted(by_inv.items()):
            lines.append(f"\n## {inv} ({len(obs_list)})\n")
            quality_counts = Counter(o.evidence_quality for o in obs_list)
            lines.append(f"Evidence quality: {dict(quality_counts)}\n")

            for obs in obs_list[:20]:  # cap at 20 per invariant for readability
                raw = obs.raw or {}
                if "filepath" in raw:
                    lines.append(f"- `{raw['filepath']}`: {raw.get('original_author', '?')} -> {raw.get('duplicate_author', raw.get('modifier', '?'))}")
                elif "branch" in raw:
                    lines.append(f"- `{raw['branch']}`: {raw.get('classification', raw.get('first_author', ''))}")
                elif "card" in raw:
                    lines.append(f"- Card #{raw['card']}: {raw.get('moved_by', raw.get('unlinked_by', ''))}")
                else:
                    lines.append(f"- {obs.description[:100] if obs.description else inv}")
            if len(obs_list) > 20:
                lines.append(f"- ... and {len(obs_list) - 20} more")
            lines.append("")

    with open(path, "w") as f:
        f.write("\n".join(lines))


def _write_timeline(state: PipelineState, config: PipelineConfig, out: str) -> None:
    """Write timeline.md — chronological summary."""
    path = os.path.join(out, "timeline.md")
    lines = ["# Unified Timeline\n"]
    lines.append(f"**{len(state.timeline)} events from {len(set(e.source for e in state.timeline))} sources**\n")

    # Group by date
    by_date: dict[str, list] = {}
    for e in state.timeline:
        ts = e.timestamp
        if ts.tzinfo:
            date_str = ts.strftime("%Y-%m-%d")
        else:
            date_str = ts.strftime("%Y-%m-%d")
        by_date.setdefault(date_str, []).append(e)

    for date_str in sorted(by_date.keys()):
        events = by_date[date_str]
        sources = Counter(e.source for e in events)
        lines.append(f"\n## {date_str} ({len(events)} events)")
        lines.append(f"Sources: {dict(sources)}\n")

    with open(path, "w") as f:
        f.write("\n".join(lines))
