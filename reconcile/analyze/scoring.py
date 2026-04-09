"""Batch scoring — maps invariant violations to perpetrator/victim profiles.

Takes observations from invariant checks, groups by member, computes scores.
Different from the live engine's analyzer.py (which scores from real-time detector flags).
Both should converge on direction — divergence highlights edge cases.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ..config import PipelineConfig
from ..normalize.types import Observation


# Invariant violation → score mapping
# perpetrator-type: the actor did something suspicious
# victim-type: the actor's work was affected by someone else
PERPETRATOR_VIOLATIONS = {
    "zero-commit-complete": 2,
    "branch-delete-before-merge": 2,
    "file-duplicate-content": 2,
    "completion-without-assignee": 1,
    "unrecorded-branch-delete": 2,
    "batch-completion": 1,
    # Generic fallback for invariant names from invariants.py
    "attribution-preservation": 2,
    "branch-integrity": 2,
    "completion-integrity": 2,
}

VICTIM_VIOLATIONS = {
    "branch-deleted-by-other": 2,
    "file-reattributed-away": 2,
    "attribution-preservation": 2,  # victim side: actor[1] if present
}


@dataclass
class MemberScore:
    """Scoring profile for one member from batch analysis."""
    member: str
    perpetrator_score: int = 0
    victim_score: int = 0
    direction: str = "neutral"
    flags: list[dict] = field(default_factory=list)
    observation_count: int = 0


def compute(observations: list[Observation], dag, provenance, config: PipelineConfig) -> dict:
    """Score members from invariant observations.

    Returns dict of {member_name: MemberScore} serialized to dicts.
    """
    profiles: dict[str, MemberScore] = {}

    for obs in observations:
        if not obs.actors:
            continue

        invariant = obs.invariant
        # Primary actor (first in list) is the perpetrator candidate
        primary = obs.actors[0]
        if primary not in profiles:
            profiles[primary] = MemberScore(member=primary)

        p = profiles[primary]
        p.observation_count += 1

        # Check if this is a perpetrator-type violation
        weight = PERPETRATOR_VIOLATIONS.get(invariant, 0)
        if weight:
            p.perpetrator_score += weight
            p.flags.append({
                "type": invariant,
                "role": "perpetrator",
                "weight": weight,
                "date": obs.date.isoformat() if obs.date else None,
                "detail": obs.description,
                "evidence_quality": obs.evidence_quality,
                "entities": obs.entities,
            })

        # If there's a secondary actor (victim), score them too
        if len(obs.actors) > 1:
            victim = obs.actors[1]
            if victim not in profiles:
                profiles[victim] = MemberScore(member=victim)
            v = profiles[victim]
            v_weight = VICTIM_VIOLATIONS.get(invariant, 0)
            if v_weight:
                v.victim_score += v_weight
                v.flags.append({
                    "type": invariant,
                    "role": "victim",
                    "weight": v_weight,
                    "date": obs.date.isoformat() if obs.date else None,
                    "detail": obs.description,
                    "evidence_quality": obs.evidence_quality,
                    "entities": obs.entities,
                })

    # Classify direction
    for member, p in profiles.items():
        if p.perpetrator_score > 0 and p.victim_score == 0:
            p.direction = "perpetrator"
        elif p.victim_score > 0 and p.perpetrator_score == 0:
            p.direction = "victim"
        elif p.perpetrator_score > 0 and p.victim_score > 0:
            p.direction = "mixed"
        else:
            p.direction = "neutral"

    # Serialize
    return {
        name: {
            "member": p.member,
            "perpetrator_score": p.perpetrator_score,
            "victim_score": p.victim_score,
            "direction": p.direction,
            "observation_count": p.observation_count,
            "flags": p.flags,
        }
        for name, p in sorted(profiles.items(), key=lambda x: -(x[1].perpetrator_score + x[1].victim_score))
    }
