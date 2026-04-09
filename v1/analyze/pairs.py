"""SNA pair analysis — (actor, beneficiary) pairs, chain detection, hub deduplication.

Algorithms:
- build_action_pairs: construct directed pairs from violation data
- _pair_direction: Harary (1953) type-consistency classification
- detect_chains: transitive chain detection with type-coherence filtering
- compute_pair_sna: typed-edge decomposition, weighted degree, reciprocity
"""

from __future__ import annotations

from collections import Counter, defaultdict

from ..normalize.types import Event


def compute(scores: dict, board_events: list[Event]) -> dict:
    """Compute pair metrics from scored violations.

    If raw violations are available (via compute._raw_violations), builds
    full pair analysis. Otherwise returns empty.
    """
    raw_violations = getattr(compute, "_raw_violations", [])
    if not raw_violations:
        return {}

    by_inv = _index_violations(raw_violations)
    pm_name = getattr(compute, "_pm_name", "")

    all_pairs = build_action_pairs(by_inv, pm_name)
    chains = detect_chains(all_pairs)
    sna = compute_pair_sna(all_pairs)

    return {
        "pairs": {str(k): v for k, v in all_pairs.items()},
        "chains": chains,
        "sna": sna,
    }


def _index_violations(violations: list[dict]) -> dict[str, list]:
    """Index violations by invariant name."""
    by_inv: dict[str, list] = defaultdict(list)
    for v in violations:
        by_inv[v.get("invariant", "")].append(v)
    return by_inv


def build_action_pairs(by_inv: dict[str, list], pm_name: str = "") -> dict:
    """Build (actor, beneficiary) pairs from indexed violations.

    Returns dict of {(actor, beneficiary): {count, actions}}.
    """
    all_pairs: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "actions": []})

    # Inv 7: completion moves (non-PM)
    for v in by_inv.get("completion-attribution", []):
        if v.get("mover_is_pm"):
            continue
        for bene in v.get("assigned_to", []):
            if v["moved_by"] != bene:
                key = (v["moved_by"], bene)
                all_pairs[key]["count"] += 1
                all_pairs[key]["actions"].append({"type": "completion", "date": v.get("date", ""), "card": v.get("card")})

    # Inv 6: dependency unlinks (non-PM)
    for v in by_inv.get("dependency-unlinking", []):
        if v.get("unlinked_by") == pm_name:
            continue
        if v.get("unlinked_by") != v.get("linked_by"):
            key = (v["unlinked_by"], v["linked_by"])
            all_pairs[key]["count"] += 1
            all_pairs[key]["actions"].append({"type": "unlink", "date": v.get("unlinked_date", ""), "card": v.get("card")})

    # Inv 2: branch deletions (deleted-by-other)
    for v in by_inv.get("branch-deletion-transparency", []):
        if v.get("classification") == "deleted-by-other":
            for ev in v.get("board_delgithub_events", []):
                if ev.get("by") and ev["by"] != v.get("first_author"):
                    key = (ev["by"], v["first_author"])
                    all_pairs[key]["count"] += 1
                    all_pairs[key]["actions"].append({"type": "branch-delete", "date": ev.get("date", ""), "branch": v.get("branch")})

    return dict(all_pairs)


def _pair_direction(pair_data: dict) -> str:
    """Classify pair direction: beneficial, adversarial, mixed, or untyped.

    Per Harary (1953): mixed-sign paths are incoherent and should not
    form transitive chains.
    """
    actions = pair_data.get("actions", [])
    beneficial = sum(1 for a in actions if a.get("type") == "completion")
    adversarial = sum(1 for a in actions if a.get("type") in ("branch-delete", "unlink"))
    if beneficial == 0 and adversarial == 0:
        return "untyped"
    if beneficial > 0 and adversarial == 0:
        return "beneficial"
    if adversarial > 0 and beneficial == 0:
        return "adversarial"
    return "mixed"


def detect_chains(all_pairs: dict, min_link: int = 3) -> list[dict]:
    """Detect transitive (actor -> hub -> beneficiary) chains.

    Both links must meet min_link threshold with same coherent direction.
    Hub deduplication: if a node is hub in >2 chains, keep only strongest.
    """
    pair_chains = []
    pair_keys = set(all_pairs.keys())
    for (a, b) in pair_keys:
        for (c, d) in pair_keys:
            if b == c and a != d:
                count_ab = all_pairs[(a, b)]["count"]
                count_bd = all_pairs[(b, d)]["count"]
                if count_ab >= min_link and count_bd >= min_link:
                    dir_ab = _pair_direction(all_pairs[(a, b)])
                    dir_bd = _pair_direction(all_pairs[(b, d)])
                    coherent = (
                        (dir_ab == dir_bd and dir_ab != "mixed") or
                        (dir_ab == "untyped" and dir_bd != "mixed") or
                        (dir_bd == "untyped" and dir_ab != "mixed")
                    )
                    if coherent:
                        effective_dir = dir_ab if dir_ab != "untyped" else dir_bd
                        pair_chains.append({
                            "chain": [a, b, d],
                            "links": [(a, b, count_ab), (b, d, count_bd)],
                            "total_actions": min(count_ab, count_bd),
                            "direction": effective_dir,
                        })

    # Hub deduplication
    hub_counts = Counter(c["chain"][1] for c in pair_chains)
    for hub, count in hub_counts.items():
        if count > 2:
            hub_chains = [c for c in pair_chains if c["chain"][1] == hub]
            best = max(hub_chains, key=lambda c: c["total_actions"])
            pair_chains = [c for c in pair_chains if c["chain"][1] != hub] + [best]

    return pair_chains


def compute_pair_sna(all_pairs: dict) -> dict:
    """Compute SNA metrics: typed layers, weighted degree, reciprocity."""
    BENEFICIAL_TYPES = {"completion"}
    ADVERSARIAL_TYPES = {"branch-delete", "unlink"}

    typed_layers = {"beneficial": {}, "adversarial": {}}
    for (a, b), data in all_pairs.items():
        for layer, type_set in [("beneficial", BENEFICIAL_TYPES), ("adversarial", ADVERSARIAL_TYPES)]:
            count = sum(1 for act in data["actions"] if act["type"] in type_set)
            if count > 0:
                typed_layers[layer][(a, b)] = count

    out_strength: dict[str, int] = defaultdict(int)
    in_strength: dict[str, int] = defaultdict(int)
    for (a, b), data in all_pairs.items():
        out_strength[a] += data["count"]
        in_strength[b] += data["count"]

    reciprocal = []
    seen: set[tuple] = set()
    for (a, b) in all_pairs:
        if (b, a) in all_pairs and (b, a) not in seen:
            ab_types: dict[str, int] = defaultdict(int)
            ba_types: dict[str, int] = defaultdict(int)
            for act in all_pairs[(a, b)]["actions"]:
                ab_types[act["type"]] += 1
            for act in all_pairs[(b, a)]["actions"]:
                ba_types[act["type"]] += 1
            shared = []
            for t in set(ab_types) & set(ba_types):
                if ab_types[t] >= 3 and ba_types[t] >= 3:
                    shared.append({"type": t, "forward": ab_types[t], "reverse": ba_types[t]})
            if shared:
                reciprocal.append({"pair": (a, b), "shared_types": shared})
            seen.add((a, b))
            seen.add((b, a))

    return {
        "typed_layers": {k: {str(pair): cnt for pair, cnt in v.items()} for k, v in typed_layers.items()},
        "strength": {"out": dict(out_strength), "in": dict(in_strength)},
        "reciprocal_pairs": reciprocal,
    }
