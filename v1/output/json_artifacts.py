"""JSON output — serialize pipeline state to files."""

from __future__ import annotations

import json
import os
from datetime import datetime

from ..normalize.types import PipelineState
from ..config import PipelineConfig


def _save(data, path: str) -> None:
    """Write JSON with default serialization for datetime objects."""
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def write(state: PipelineState, config: PipelineConfig) -> None:
    """Write all JSON artifacts to output directory."""
    out = config.output_dir
    os.makedirs(out, exist_ok=True)

    raw = state.raw_artifacts

    # Raw artifacts from ingest
    if "file_history" in raw:
        _save(raw["file_history"], os.path.join(out, "file-authorship.json"))
    if "file_duplicates" in raw:
        _save(raw["file_duplicates"], os.path.join(out, "file-duplicates.json"))
    if "branch_state" in raw:
        _save(raw["branch_state"], os.path.join(out, "branch-state.json"))
    if "card_data" in raw:
        _save(raw["card_data"], os.path.join(out, "card-attribution.json"))

    # Violations from analysis
    if hasattr(state, 'observations') and state.observations:
        violations = [obs.raw for obs in state.observations if obs.raw]
        _save(violations, os.path.join(out, "all-violations.json"))

    # Scores
    if state.scores:
        _save(state.scores, os.path.join(out, "scores.json"))

    # Pairs
    if state.pairs:
        _save(state.pairs, os.path.join(out, "pairs.json"))

    # DAG
    if state.dag:
        # Don't write the full DAG — just summary stats
        _save({
            "commit_count": len(state.dag.get("info", {})),
            "edge_count": sum(len(v) for v in state.dag.get("parent_to_children", {}).values()),
        }, os.path.join(out, "dag-summary.json"))

    # Provenance
    if state.provenance:
        _save(state.provenance, os.path.join(out, "branch-provenance.json"))

    # Classifications
    if state.classifications:
        _save(state.classifications, os.path.join(out, "discord-classification.json"))

    # Snowflake validation
    if state.snowflake_validation:
        _save(state.snowflake_validation, os.path.join(out, "snowflake-validation.json"))

    # Evidence manifest
    if state.manifest:
        _save(state.manifest, os.path.join(out, "evidence-manifest.json"))

    # Cross-reference summary
    summary = {
        "generated": datetime.now().isoformat(),
        "commits": len(state.commits),
        "branches": len(state.branches),
        "files": len(state.files),
        "board_events": len(state.board_events),
        "cards": len(state.cards),
        "messages": len(state.messages),
        "reports": len(state.reports),
        "timeline_events": len(state.timeline),
        "observations": len(state.observations),
    }
    _save(summary, os.path.join(out, "cross-reference.json"))
