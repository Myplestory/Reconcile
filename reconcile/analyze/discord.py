"""Message content classification — channel-agnostic tiered analysis.

Tier 1: keyword codebook (deterministic substring matching)
Tier 2: regex patterns (broader, documented false-positive risk)

Methodology: dictionary-based, LIWC-comparable. NOT NLP.
Codebook is overridable via config.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ..config import PipelineConfig
from ..normalize.types import Message

# Default codebook — generic keywords for software team communication
DEFAULT_TIER1 = {
    "proactive": {
        "definition": "Offers to perform work or take initiative, unprompted",
        "keywords": [
            "i can", "i will", "i'll", "let me", "i made", "i wrote",
            "i set up", "i created", "i fixed", "i pushed", "i deployed",
            "i updated", "i merged", "i added", "i built", "i refactored",
        ],
    },
    "outreach": {
        "definition": "Proactively checking on teammates or offering assistance",
        "keywords": [
            "do you need", "need help", "need any help", "want me to",
            "i can help", "let me know if", "are you stuck", "are you good",
            "how are you doing", "want help", "can i help",
        ],
    },
    "technical_help": {
        "definition": "Guidance, explanation, or instruction directed at others",
        "keywords": [
            "you can", "you need to", "you should", "try this", "should work",
            "heres how", "like this", "for example", "the issue is",
            "the problem is", "that happens because", "the fix is",
        ],
    },
    "code_git": {
        "definition": "References to code artifacts, branches, commits, tools, or files",
        "keywords": [
            "branch", "commit", "merge", "pull request", "pr ",
            "git ", "npm", "deploy", "ssh", "database",
            "api/", "localhost", "server", "component",
        ],
    },
    "resource_link": {"definition": "URL in message", "match_rule": "has_link"},
    "question": {"definition": "Contains a question mark", "match_rule": "has_question_mark"},
    "coordination": {
        "definition": "Meeting logistics, sprint references, or deadline management",
        "keywords": [
            "meeting", "sprint", "deadline", "by tomorrow", "by tonight",
            "before class", "standup", "end of day", "this week", "next week",
        ],
    },
    "attachment": {"definition": "File upload in message", "match_rule": "has_attachment"},
}

DEFAULT_TIER2 = {
    "candidate_proactive": {
        "patterns": [
            r"\bi('ll| will| can) .{0,20}(help|fix|look|check|push|deploy|handle)",
            r"\b(want me to|shall i|should i) ",
            r"\bi (already|just) (did|made|fixed|pushed|merged|deployed|set up)",
        ],
    },
    "candidate_outreach": {
        "patterns": [
            r"\b(do|did) you (need|want|get|figure|manage)",
            r"\b(how('s| is| are) (it|that|the|your))",
            r"\b(lmk|let me know)",
        ],
    },
    "candidate_teaching": {
        "patterns": [
            r"\b(the reason|because|what happens is|the way .{0,10} works)",
            r"\b(so basically|essentially|in short|to clarify)",
        ],
    },
}


def _classify_tier1(content: str, has_link: bool, has_attachment: bool, codebook: dict) -> dict:
    """Tier 1: keyword codebook. Returns {category: matched_keyword}."""
    lower = content.lower()
    matches = {}
    for cat, defn in codebook.items():
        if "match_rule" in defn:
            rule = defn["match_rule"]
            if rule == "has_link" and has_link:
                matches[cat] = "[URL]"
            elif rule == "has_question_mark" and "?" in content:
                matches[cat] = "[?]"
            elif rule == "has_attachment" and has_attachment:
                matches[cat] = "[attachment]"
        elif "keywords" in defn:
            for kw in defn["keywords"]:
                if kw in lower:
                    matches[cat] = kw
                    break
    return matches


def _classify_tier2(content: str, patterns: dict) -> dict:
    """Tier 2: regex patterns. Returns {category: {pattern, matched}}."""
    lower = content.lower()
    matches = {}
    for cat, defn in patterns.items():
        for pattern in defn.get("patterns", []):
            m = re.search(pattern, lower)
            if m:
                matches[cat] = {"pattern": pattern, "matched": m.group(0)}
                break
    return matches


def classify(messages: list[Message], config: PipelineConfig) -> dict:
    """Classify all messages using 2-tier codebook.

    Returns dict with per-member statistics, codebook, and methodology.
    """
    codebook = DEFAULT_TIER1
    tier2_patterns = DEFAULT_TIER2

    stats: dict[str, dict] = defaultdict(lambda: {
        "tier1": defaultdict(int),
        "tier2": defaultdict(int),
        "total": 0,
    })

    for msg in messages:
        has_link = "http" in msg.content.lower()
        has_attachment = bool(msg.raw.get("attachments"))

        t1 = _classify_tier1(msg.content, has_link, has_attachment, codebook)
        t2 = _classify_tier2(msg.content, tier2_patterns)

        author = msg.author
        stats[author]["total"] += 1
        for cat in t1:
            stats[author]["tier1"][cat] += 1
        for cat in t2:
            stats[author]["tier2"][cat] += 1

        msg.tier1_categories = list(t1.keys())
        msg.tier2_candidates = list(t2.keys())

    return {
        "per_member": {k: dict(v) for k, v in stats.items()},
        "codebook": {k: v.get("definition", "") for k, v in codebook.items()},
        "total_messages": len(messages),
        "methodology": "Dictionary-based 2-tier classification (LIWC-comparable)",
    }
