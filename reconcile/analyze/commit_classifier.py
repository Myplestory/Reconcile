"""NLI-enhanced commit and card classification.

Deterministic-baseline-plus-NLI-enhancement architecture. The deterministic
baseline (keyword + diff categories + CC prefix + diff size) ALWAYS runs.
NLI is an optional enhancement that adds probabilistic confidence when the
model is available and confident.

Academic foundations:
    Yin et al. (2019), "Benchmarking Zero-shot Text Classification", EMNLP
    He et al. (2021), "DeBERTa: Decoding-enhanced BERT", ICLR
    Mockus & Votta (2000), "Identifying Reasons for Software Changes", ICSM
    Levin & Yehudai (2017), "Boosting Automatic Commit Classification", JSS
    Herzig & Zeller (2013), "It's Not a Bug, It's a Feature", ICSE
    Nygard (2007), "Release It!" — circuit breaker pattern

See docs/methodology.md for full rationale.
See docs/system.md for architecture details.
See docs/hypothesis.md for research hypotheses.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .code_quality import CommitAnalysis

log = logging.getLogger(__name__)

# --- Guarded ML imports ---

try:
    import torch
    from transformers import AutoTokenizer, AutoModelForSequenceClassification
    NLI_AVAILABLE = True
except ImportError:
    NLI_AVAILABLE = False


# ============================================================
# Circuit Breaker (Nygard 2007)
# ============================================================

class CircuitBreaker:
    """Three-state circuit breaker for NLI inference.

    CLOSED   -> normal operation, NLI scores returned
    OPEN     -> NLI bypassed, all calls route to deterministic immediately
    HALF_OPEN -> test recovery with single request

    Thread safety: all methods called from asyncio event loop (single-threaded).
    The executor only runs _score_batch_sync which has no circuit reference.
    No lock needed — serialization is structural.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout: float = 60.0,
        success_threshold: int = 2,
    ):
        self.state = self.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = 0.0
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold

    def allow_request(self) -> bool:
        if self.state == self.CLOSED:
            return True
        if self.state == self.OPEN:
            if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                self.state = self.HALF_OPEN
                self._success_count = 0
                return True
            return False
        # HALF_OPEN: allow test request
        return True

    def record_success(self) -> None:
        self._failure_count = 0
        if self.state == self.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self.state = self.CLOSED
                log.info("NLI circuit breaker CLOSED (recovered)")

    def record_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self.state == self.HALF_OPEN:
            self.state = self.OPEN
            log.warning("NLI circuit breaker OPEN (half-open test failed)")
        elif self._failure_count >= self.failure_threshold:
            self.state = self.OPEN
            log.warning("NLI circuit breaker OPEN after %d failures", self._failure_count)

    def force_open(self) -> None:
        """Force open — used when model fails to load at startup."""
        self.state = self.OPEN
        self._last_failure_time = time.monotonic()


# ============================================================
# Inference Engine
# ============================================================

class InferenceEngine:
    """Manages DeBERTa-v3-base lifecycle for NLI scoring.

    Singleton per orchestrator. Shared across all teams.
    All inference runs in ThreadPoolExecutor — event loop never blocks.
    FP32 only — single model has no VRAM pressure.

    Adapted from PolyEdge CrossEncoder:
    - Eager load at startup (not lazy on first request)
    - Device auto-detection (cuda -> mps -> cpu)
    - torch.compile() with platform guards
    - GPU semaphore for concurrent team serialization
    - Circuit breaker for fail-close behavior
    """

    def __init__(
        self,
        model_name: str = "MoritzLaurer/deberta-v3-base-zeroshot-v2.0",
        batch_size: int = 64,
        inference_timeout: float = 10.0,
        semaphore_timeout: float = 30.0,
    ):
        self._model = None
        self._tokenizer = None
        self._device: str = "cpu"
        self._lock = asyncio.Lock()
        self._gpu_semaphore = asyncio.Semaphore(1)
        self._circuit = CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=60.0,
            success_threshold=2,
        )
        self._initialized = False
        self.model_name = model_name
        self.batch_size = batch_size
        self.inference_timeout = inference_timeout
        self.semaphore_timeout = semaphore_timeout

    @property
    def circuit(self) -> CircuitBreaker:
        return self._circuit

    async def initialize(self) -> None:
        """Load model eagerly. Call from Orchestrator.start().

        Fail-fast: OOM/driver errors surface here, not on first event.
        If load fails, circuit forced OPEN — all calls route to deterministic.
        """
        if not NLI_AVAILABLE:
            log.warning("torch/transformers not installed — NLI unavailable")
            self._circuit.force_open()
            return

        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            try:
                loop = asyncio.get_running_loop()
                self._model, self._tokenizer = await loop.run_in_executor(
                    None, self._load_model_sync
                )
                await self._warmup()
                self._initialized = True
                log.info("NLI engine ready: %s on %s", self.model_name, self._device)
            except Exception as e:
                log.error("NLI model load failed: %s — running keyword-only", e)
                self._circuit.force_open()

    def _detect_device(self) -> str:
        if torch.cuda.is_available():
            return "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    def _load_model_sync(self):
        self._device = self._detect_device()

        tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModelForSequenceClassification.from_pretrained(self.model_name)
        model.to(self._device)
        model.eval()

        # torch.compile() — platform-aware guards
        if self._device == "cuda":
            try:
                model = torch.compile(model, mode="reduce-overhead")
                log.info("torch.compile() applied (CUDA reduce-overhead)")
            except Exception as e:
                log.debug("torch.compile() skipped: %s", e)
        # MPS: disabled — Metal shader compilation errors
        # CPU: no benefit at this batch size

        return model, tokenizer

    async def _warmup(self) -> None:
        """Dummy forward pass to trigger JIT compilation before real traffic."""
        dummy = [("test commit message", "This change adds new functionality.")]
        await asyncio.get_running_loop().run_in_executor(
            None, self._score_batch_sync, dummy
        )

    async def score_batch(self, pairs: list[tuple[str, str]]) -> list[dict[str, float]] | None:
        """Score NLI pairs. Returns None if circuit OPEN or timeout.

        Caller MUST handle None by using deterministic fallback.
        Two separate timeouts (congestion != failure):
            1. Semaphore timeout (30s) — NOT a circuit breaker failure.
            2. Inference timeout (10s) — IS a circuit breaker failure.
        """
        if not self._circuit.allow_request():
            return None

        try:
            await asyncio.wait_for(
                self._gpu_semaphore.acquire(),
                timeout=self.semaphore_timeout,
            )
        except asyncio.TimeoutError:
            log.info("NLI semaphore congested (>%.0fs) — deterministic fallback",
                     self.semaphore_timeout)
            return None  # NOT a circuit failure

        try:
            loop = asyncio.get_running_loop()
            scores = await asyncio.wait_for(
                loop.run_in_executor(None, self._score_batch_sync, pairs),
                timeout=self.inference_timeout,
            )
            self._circuit.record_success()
            return scores
        except asyncio.TimeoutError:
            log.warning("NLI inference timed out after %.1fs", self.inference_timeout)
            self._circuit.record_failure()
            return None
        except Exception as e:
            log.error("NLI inference failed: %s", e)
            self._circuit.record_failure()
            return None
        finally:
            self._gpu_semaphore.release()

    def _score_batch_sync(self, pairs: list[tuple[str, str]]) -> list[dict[str, float]]:
        """Synchronous batch scoring. Runs in executor, never on event loop.

        Micro-batches of self.batch_size to bound peak memory.
        Direct tokenizer + model.forward(), not HF pipeline.
        """
        all_scores: list[dict[str, float]] = []
        for start in range(0, len(pairs), self.batch_size):
            chunk = pairs[start:start + self.batch_size]
            inputs = self._tokenizer(
                [p[0] for p in chunk], [p[1] for p in chunk],
                padding=True, truncation=True, max_length=256,
                return_tensors="pt",
            ).to(self._device)

            with torch.no_grad():
                outputs = self._model(**inputs)
                probs = torch.softmax(outputs.logits, dim=-1)

            id2label = self._model.config.id2label
            for row in probs:
                score = {id2label[i]: row[i].item() for i in range(len(row))}
                all_scores.append(score)

        return all_scores

    async def shutdown(self) -> None:
        """Release model memory. Called from Orchestrator.shutdown()."""
        if self._model is not None:
            del self._model
            del self._tokenizer
            self._model = None
            self._tokenizer = None
            if NLI_AVAILABLE and torch.cuda.is_available():
                torch.cuda.empty_cache()
            import gc
            gc.collect()
            log.info("NLI engine shut down, memory released")


# ============================================================
# Canonicalization
# ============================================================

_CC_PREFIX = re.compile(
    r'^(feat|fix|refactor|docs|chore|style|test|perf|ci|build|revert)'
    r'(\(.+?\))?[!]?:\s*(.+)',
    re.IGNORECASE,
)

_FILE_REF = re.compile(
    r'\b\w+\.(php|jsx?|tsx?|css|py|go|rs|java|rb|sql|html|vue|svelte)\b',
    re.IGNORECASE,
)

_GITHUB_DEFAULTS = frozenset({
    "add files via upload",
    "initial commit",
    "first commit",
})


@dataclass
class CanonicalCommit:
    """Canonicalized commit for NLI scoring."""
    prefix: str | None = None      # CC prefix (feat, fix, etc.)
    scope: str | None = None       # CC scope (auth, api, etc.)
    body: str = ""                 # cleaned message body
    enrichment: str = ""           # diff metadata enrichment
    degenerate: bool = False       # too short/generic for NLI
    raw: str = ""                  # original message


def canonicalize_commit(
    message: str,
    diff_categories: list[str] | None = None,
    total_adds: int = 0,
    total_dels: int = 0,
    file_count: int = 0,
) -> CanonicalCommit:
    """Canonicalize a commit message for NLI scoring.

    Stages:
      1. Parse Conventional Commits prefix (if present)
      2. Strip file references (noise for entailment)
      3. Enrich with diff metadata (observable facts)
      4. Flag degenerate messages (<4 words, GitHub defaults)
    """
    raw = message.strip()
    msg = raw

    # Stage 1: Parse CC prefix
    prefix = None
    scope = None
    m = _CC_PREFIX.match(msg)
    if m:
        prefix = m.group(1).lower()
        scope = m.group(2).strip("()") if m.group(2) else None
        msg = m.group(3).strip()

    # Stage 2: Strip file references
    body = _FILE_REF.sub("", msg).strip()
    # Collapse multiple spaces
    body = re.sub(r'\s+', ' ', body).strip()

    # Stage 3: Enrich with diff metadata
    enrichment = ""
    if diff_categories and file_count > 0:
        unique_cats = sorted(set(diff_categories))
        cat_str = " and ".join(unique_cats[:4])
        enrichment = f"Modified {file_count} file(s) in {cat_str} (+{total_adds} -{total_dels})"

    # Stage 4: Degenerate check
    # Use raw message word count if CC prefix was stripped (prefix contributes context)
    check_text = raw if prefix else body
    words = check_text.split()
    degenerate = False
    if len(words) < 4:
        degenerate = True
    elif raw.lower().rstrip(".") in _GITHUB_DEFAULTS:
        degenerate = True
    elif re.match(r'^(update|create|delete|add)\s+\S+$', raw, re.IGNORECASE):
        degenerate = True

    return CanonicalCommit(
        prefix=prefix,
        scope=scope,
        body=body,
        enrichment=enrichment,
        degenerate=degenerate,
        raw=raw,
    )


def canonicalize_card(title: str, comments: list[str] | None = None) -> CanonicalCommit:
    """Canonicalize a card title for NLI scoring.

    Simpler than commit canonicalization — no CC prefix, no diff metadata.
    """
    raw = title.strip()
    body = raw

    # Strip generic patterns: "{Name} - User story {N}"
    body = re.sub(r'^[\w\s]+ - User story \d+\s*', '', body, flags=re.IGNORECASE).strip()
    # Strip trailing (frontend)/(backend) labels — handled by diff enrichment
    body = re.sub(r'\s*\((frontend|backend)\)\s*$', '', body, flags=re.IGNORECASE).strip()

    # Append first comment if available and body is short
    if comments and len(body.split()) < 6:
        first_comment = comments[0].strip()[:200]
        if first_comment:
            body = f"{body}. {first_comment}"

    degenerate = len(body.split()) < 4

    return CanonicalCommit(
        body=body,
        degenerate=degenerate,
        raw=raw,
    )


# ============================================================
# Hypothesis Templates
# ============================================================

COMMIT_HYPOTHESES: dict[str, str] = {
    "feature":                   "This change adds new functionality or implements a new user-facing capability.",
    "maintenance:bugfix":        "This change fixes a bug, corrects an error, or resolves broken behavior.",
    "maintenance:refactor":      "This change restructures or reorganizes existing code without changing its behavior.",
    "maintenance:documentation": "This change updates documentation, README files, or process guidelines.",
    "maintenance:dependency":    "This change adds, removes, or updates a project dependency or library.",
    "test":                      "This change adds, modifies, or fixes automated tests or test infrastructure.",
    "devops:infra":              "This change modifies deployment scripts, CI/CD pipelines, or server infrastructure.",
    "devops:config":             "This change updates build configuration, linting rules, or project settings.",
}

CARD_HYPOTHESES: dict[str, str] = {
    "feature":  "This task requires implementing new functionality or a user-facing capability.",
    "bugfix":   "This task requires fixing a bug, correcting broken behavior, or resolving an error.",
    "refactor": "This task requires restructuring or reorganizing existing code.",
    "test":     "This task requires writing or updating automated tests.",
    "docs":     "This task requires updating documentation or process guidelines.",
    "devops":   "This task requires deployment, infrastructure, or CI/CD changes.",
    "config":   "This task requires updating build configuration or project settings.",
    "research": "This task requires investigation, prototyping, or feasibility analysis.",
}


# ============================================================
# Deterministic Classification (always runs)
# ============================================================

# CC prefix -> category mapping (authoritative when present)
CC_MAP: dict[str, str] = {
    "feat": "feature",
    "fix": "maintenance:bugfix",
    "refactor": "maintenance:refactor",
    "docs": "maintenance:documentation",
    "chore": "devops:config",
    "style": "maintenance:refactor",
    "test": "test",
    "perf": "maintenance:refactor",
    "ci": "devops:infra",
    "build": "devops:config",
    "revert": "maintenance:bugfix",
}

# Keyword regexes (moved from code_quality.py)
_REFACTOR_KW = re.compile(
    r'refactor|consolidat|standardiz|shared module|cleanup|clean up|restructur|migrat|extract|normaliz',
    re.IGNORECASE,
)
_BUGFIX_KW = re.compile(
    r'\bfix\b|hotfix|bug\b|resolv|correct|patch|repair|broken',
    re.IGNORECASE,
)
_FEATURE_KW = re.compile(
    r'\badd\b|implement|introduc|creat|new\s+(feature|page|endpoint|component)',
    re.IGNORECASE,
)
_DOCS_KW = re.compile(
    r'\bdocs?\b|readme|documentation|comment',
    re.IGNORECASE,
)
_TEST_KW = re.compile(
    r'\btest\b|testing|spec\b|coverage',
    re.IGNORECASE,
)
_DEVOPS_KW = re.compile(
    r'deploy|docker|ci[/ ]cd|pipeline|infra|nginx|config|\.env',
    re.IGNORECASE,
)

# Card-specific keywords
_CARD_FEATURE_KW = re.compile(
    r'implement|create|build|develop|design|add|new',
    re.IGNORECASE,
)
_CARD_BUGFIX_KW = re.compile(
    r'\bfix\b|bug|resolv|broken|error|issue',
    re.IGNORECASE,
)
_CARD_RESEARCH_KW = re.compile(
    r'research|investigat|prototype|feasibility|spike|explor',
    re.IGNORECASE,
)
_CARD_TEST_KW = re.compile(
    r'\btests?\b|testing|QA|quality assurance',
    re.IGNORECASE,
)


def classify_deterministic(
    canonical: CanonicalCommit,
    diff_categories: list[str] | None = None,
    diff_size: int = 0,
) -> dict:
    """Deterministic classification using keyword + diff + CC prefix.

    Always runs. Always produces a result. Fully auditable — every
    rule is a regex pattern or lookup table.

    Returns {category, source, confidence, keyword_result, cc_result}.
    """
    result: dict = {
        "category": "other",
        "source": "heuristic",
        "confidence": None,
        "keyword_result": None,
        "cc_result": None,
        "diff_result": None,
    }

    # Signal 1: CC prefix (authoritative when present)
    if canonical.prefix and canonical.prefix in CC_MAP:
        result["cc_result"] = CC_MAP[canonical.prefix]

    # Signal 2: Keyword matching
    msg = canonical.body or canonical.raw
    if _REFACTOR_KW.search(msg):
        result["keyword_result"] = "maintenance:refactor"
    elif _BUGFIX_KW.search(msg):
        result["keyword_result"] = "maintenance:bugfix"
    elif _FEATURE_KW.search(msg):
        result["keyword_result"] = "feature"
    elif _DOCS_KW.search(msg):
        result["keyword_result"] = "maintenance:documentation"
    elif _TEST_KW.search(msg):
        result["keyword_result"] = "test"
    elif _DEVOPS_KW.search(msg):
        result["keyword_result"] = "devops:config"

    # Signal 3: Diff file categories
    if diff_categories:
        cat_counts: dict[str, int] = defaultdict(int)
        for cat in diff_categories:
            if cat:
                master = cat.split(":")[0]
                cat_counts[master] += 1
        if cat_counts:
            dominant = max(cat_counts, key=cat_counts.get)
            # Map diff master to classification category
            diff_map = {
                "devops": "devops:config",
                "maintenance": "maintenance:documentation",
                "test": "test",
            }
            result["diff_result"] = diff_map.get(dominant, dominant)

    # Priority: CC prefix > keyword > diff > fallback
    if result["cc_result"]:
        result["category"] = result["cc_result"]
    elif result["keyword_result"]:
        result["category"] = result["keyword_result"]
    elif result["diff_result"]:
        result["category"] = result["diff_result"]
    elif diff_size and diff_size < 20:
        result["category"] = "maintenance:bugfix"
    else:
        result["category"] = "other"

    return result


def classify_card_deterministic(
    canonical: CanonicalCommit,
    pipeline_name: str = "",
) -> dict:
    """Deterministic classification for card titles.

    Weaker than commit deterministic (no diff metadata).
    Marked source: 'heuristic-weak'.
    """
    result: dict = {
        "category": "feature",  # default for cards
        "source": "heuristic-weak",
        "confidence": None,
        "keyword_result": None,
    }

    title = canonical.body or canonical.raw
    if _CARD_RESEARCH_KW.search(title):
        result["keyword_result"] = "research"
    elif _CARD_TEST_KW.search(title):
        result["keyword_result"] = "test"
    elif _CARD_BUGFIX_KW.search(title):
        result["keyword_result"] = "bugfix"
    elif _CARD_FEATURE_KW.search(title):
        result["keyword_result"] = "feature"

    if result["keyword_result"]:
        result["category"] = result["keyword_result"]
    elif pipeline_name.lower() in ("testing", "qa"):
        result["category"] = "test"

    return result


# ============================================================
# Multi-Signal Fusion
# ============================================================

# Base weights (Levin & Yehudai 2017 multi-signal approach)
FUSION_WEIGHTS = {
    "nli":       0.40,
    "cc_prefix": 0.25,
    "diff_cats": 0.20,
    "keyword":   0.10,
    "diff_size": 0.05,
}


def fuse_signals(
    nli_scores: dict[str, dict[str, float]] | None,
    cc_prefix: str | None,
    diff_categories: list[str] | None,
    keyword_match: str | None,
    diff_size: int,
    confidence_floor: float = 0.40,
    margin_threshold: float = 0.15,
) -> tuple[str, float | None, dict]:
    """Multi-signal fusion. Returns (category, confidence, details).

    When NLI is unavailable or not confident, weights redistribute
    proportionally to remaining signals.
    """
    available: dict[str, float] = {}
    signal_votes: dict[str, dict[str, float]] = {}

    # NLI signal
    nli_winner = None
    nli_confident = False
    if nli_scores:
        # Find winner and margin
        sorted_cats = sorted(nli_scores.keys(),
                             key=lambda k: nli_scores[k].get("entailment", 0),
                             reverse=True)
        if sorted_cats:
            nli_winner = sorted_cats[0]
            top_score = nli_scores[nli_winner].get("entailment", 0)
            runner_up = nli_scores[sorted_cats[1]].get("entailment", 0) if len(sorted_cats) > 1 else 0
            margin = top_score - runner_up
            nli_confident = top_score >= confidence_floor and margin >= margin_threshold

    if nli_confident and nli_winner:
        available["nli"] = FUSION_WEIGHTS["nli"]
        signal_votes["nli"] = {nli_winner: 1.0}

    # CC prefix signal
    cc_category = CC_MAP.get(cc_prefix) if cc_prefix else None
    if cc_category:
        available["cc_prefix"] = FUSION_WEIGHTS["cc_prefix"]
        signal_votes["cc_prefix"] = {cc_category: 1.0}

    # Diff categories signal
    if diff_categories:
        cat_counts: dict[str, int] = defaultdict(int)
        for cat in diff_categories:
            if cat:
                master = cat.split(":")[0]
                cat_counts[master] += 1
        total = sum(cat_counts.values())
        if total > 0:
            available["diff_cats"] = FUSION_WEIGHTS["diff_cats"]
            signal_votes["diff_cats"] = {k: v / total for k, v in cat_counts.items()}

    # Keyword signal
    if keyword_match:
        available["keyword"] = FUSION_WEIGHTS["keyword"]
        signal_votes["keyword"] = {keyword_match: 1.0}

    # Diff size signal (weak heuristic)
    if diff_size > 0:
        available["diff_size"] = FUSION_WEIGHTS["diff_size"]
        if diff_size < 20:
            signal_votes["diff_size"] = {"maintenance:bugfix": 0.6, "feature": 0.4}
        elif diff_size > 200:
            signal_votes["diff_size"] = {"feature": 0.6, "maintenance:refactor": 0.4}
        else:
            signal_votes["diff_size"] = {"feature": 0.5, "maintenance:bugfix": 0.5}

    if not available:
        return "other", None, {"signals": {}}

    # Redistribute weights proportionally
    total_weight = sum(available.values())
    normalized = {k: v / total_weight for k, v in available.items()}

    # Weighted vote aggregation
    category_scores: dict[str, float] = defaultdict(float)
    for signal_name, weight in normalized.items():
        votes = signal_votes.get(signal_name, {})
        for cat, vote_strength in votes.items():
            category_scores[cat] += weight * vote_strength

    if not category_scores:
        return "other", None, {"signals": {}}

    winner = max(category_scores, key=category_scores.get)
    confidence = nli_scores[nli_winner].get("entailment") if nli_confident and nli_winner else None

    details = {
        "signals": {
            "nli_winner": nli_winner if nli_confident else None,
            "nli_confident": nli_confident,
            "cc_prefix": cc_category,
            "keyword": keyword_match,
            "diff_categories": diff_categories,
            "diff_size": diff_size,
        },
        "category_scores": dict(category_scores),
        "weights_used": normalized,
    }

    return winner, confidence, details


# ============================================================
# CommitClassifier (orchestrates all stages)
# ============================================================

class CommitClassifier:
    """Classifies commits and cards via deterministic baseline + optional NLI.

    Usage:
        classifier = CommitClassifier(engine=engine_or_none)
        results = await classifier.classify_batch(commits)

    When engine is None, all classifications are deterministic.
    """

    def __init__(
        self,
        engine: InferenceEngine | None = None,
        hot_circuit: CircuitBreaker | None = None,
        cold_circuit: CircuitBreaker | None = None,
    ):
        self.engine = engine
        self.hot_circuit = hot_circuit or (engine.circuit if engine else CircuitBreaker())
        self.cold_circuit = cold_circuit or CircuitBreaker(
            failure_threshold=3, recovery_timeout=120.0,
        )
        self._cache: dict[str, dict] = {}  # sha -> classification

    async def classify_batch(
        self,
        commits: list[CommitAnalysis],
    ) -> dict[str, dict]:
        """Classify all commits. Returns {sha: classification_result}.

        1. Canonicalize all
        2. Deterministic classify ALL (baseline)
        3. Filter non-degenerate for NLI
        4. If engine available: score_batch()
        5. Fuse: NLI + deterministic signals
        6. Record both fused + keyword_only (calibration)
        7. Cache NLI-backed results only
        """
        from .code_quality import categorize_file

        results: dict[str, dict] = {}

        # Separate cached vs uncached
        uncached = []
        for c in commits:
            if c.sha in self._cache:
                results[c.sha] = self._cache[c.sha]
            else:
                uncached.append(c)

        if not uncached:
            return results

        # Canonicalize + deterministic for all uncached
        canonicals: list[tuple[CommitAnalysis, CanonicalCommit, dict]] = []
        for c in uncached:
            # Get diff categories
            diff_cats = []
            for fc in c.files:
                cat = categorize_file(fc.path)
                if cat:
                    diff_cats.append(cat)

            canonical = canonicalize_commit(
                message=c.message,
                diff_categories=diff_cats,
                total_adds=c.total_adds,
                total_dels=sum(c.del_counts.values()),
                file_count=len(c.files),
            )

            det = classify_deterministic(
                canonical,
                diff_categories=diff_cats,
                diff_size=c.total_adds + sum(c.del_counts.values()),
            )

            canonicals.append((c, canonical, det))

        # NLI scoring for non-degenerate commits
        nli_results: dict[str, dict[str, dict[str, float]]] = {}
        nli_eligible = [(c, can, det) for c, can, det in canonicals if not can.degenerate]

        if nli_eligible and self.engine and self.hot_circuit.allow_request():
            # Build all (premise, hypothesis) pairs
            pairs: list[tuple[str, str]] = []
            pair_map: list[tuple[str, str]] = []  # (sha, category)

            for c, can, det in nli_eligible:
                text = can.body
                if can.enrichment:
                    text = f"{text}. {can.enrichment}"
                for cat, hyp in COMMIT_HYPOTHESES.items():
                    pairs.append((text, hyp))
                    pair_map.append((c.sha, cat))

            scores = await self.engine.score_batch(pairs)
            if scores:
                # Unpack scores back to per-commit dicts
                idx = 0
                for c, can, det in nli_eligible:
                    sha_scores: dict[str, dict[str, float]] = {}
                    for cat in COMMIT_HYPOTHESES:
                        sha_scores[cat] = scores[idx]
                        idx += 1
                    nli_results[c.sha] = sha_scores

        # Fuse signals for all commits
        for c, canonical, det in canonicals:
            nli = nli_results.get(c.sha)
            diff_cats = []
            for fc in c.files:
                cat = categorize_file(fc.path)
                if cat:
                    diff_cats.append(cat)

            category, confidence, details = fuse_signals(
                nli_scores=nli,
                cc_prefix=canonical.prefix,
                diff_categories=diff_cats,
                keyword_match=det["keyword_result"],
                diff_size=c.total_adds + sum(c.del_counts.values()),
            )

            nli_contributed = nli is not None and confidence is not None

            result = {
                "sha": c.sha,
                "classification": category,
                "confidence": confidence,
                "source": "nli" if nli_contributed else ("degenerate" if canonical.degenerate else "heuristic"),
                "classification_deterministic": det["category"],
                "nli_contributed": nli_contributed,
                "agreement": category == det["category"],
                "scores": nli,
                "signals": details.get("signals", {}),
                "margin": None,
                "circuit_state": self.hot_circuit.state,
            }

            # Compute margin if NLI available
            if nli:
                sorted_ent = sorted(
                    [s.get("entailment", 0) for s in nli.values()],
                    reverse=True,
                )
                if len(sorted_ent) >= 2:
                    result["margin"] = sorted_ent[0] - sorted_ent[1]

            results[c.sha] = result

            # Cache only NLI-backed results
            if nli_contributed:
                self._cache[c.sha] = result

        return results

    async def classify_cards(
        self,
        cards: list[dict],
    ) -> dict[str, dict]:
        """Classify card titles. Returns {card_id: classification_result}.

        cards: [{card_id, title, comments?, pipeline_name?}]
        """
        results: dict[str, dict] = {}

        # Canonicalize + deterministic for all
        canonicals: list[tuple[dict, CanonicalCommit, dict]] = []
        for card in cards:
            canonical = canonicalize_card(
                title=card.get("title", ""),
                comments=card.get("comments"),
            )
            det = classify_card_deterministic(
                canonical,
                pipeline_name=card.get("pipeline_name", ""),
            )
            canonicals.append((card, canonical, det))

        # NLI scoring for non-degenerate
        nli_results: dict[str, dict[str, dict[str, float]]] = {}
        nli_eligible = [(card, can, det) for card, can, det in canonicals if not can.degenerate]

        if nli_eligible and self.engine and self.cold_circuit.allow_request():
            pairs: list[tuple[str, str]] = []
            pair_map: list[tuple[str, str]] = []

            for card, can, det in nli_eligible:
                text = can.body
                for cat, hyp in CARD_HYPOTHESES.items():
                    pairs.append((text, hyp))
                    pair_map.append((card["card_id"], cat))

            scores = await self.engine.score_batch(pairs)
            if scores:
                idx = 0
                for card, can, det in nli_eligible:
                    card_scores: dict[str, dict[str, float]] = {}
                    for cat in CARD_HYPOTHESES:
                        card_scores[cat] = scores[idx]
                        idx += 1
                    nli_results[card["card_id"]] = card_scores

        # Build results
        for card, canonical, det in canonicals:
            card_id = card["card_id"]
            nli = nli_results.get(card_id)

            # Simple fusion for cards: NLI winner if confident, else deterministic
            category = det["category"]
            confidence = None
            source = det["source"]

            if nli:
                sorted_cats = sorted(nli.keys(),
                                     key=lambda k: nli[k].get("entailment", 0),
                                     reverse=True)
                if sorted_cats:
                    top = nli[sorted_cats[0]].get("entailment", 0)
                    runner = nli[sorted_cats[1]].get("entailment", 0) if len(sorted_cats) > 1 else 0
                    if top >= 0.40 and (top - runner) >= 0.15:
                        category = sorted_cats[0]
                        confidence = top
                        source = "nli"

            results[card_id] = {
                "card_id": card_id,
                "classification": category,
                "confidence": confidence,
                "source": source,
                "classification_deterministic": det["category"],
                "nli_contributed": source == "nli",
                "agreement": category == det["category"],
                "scores": nli,
            }

        return results

    @staticmethod
    def cross_reference(
        card_classifications: dict[str, dict],
        commit_classifications: dict[str, dict],
        card_to_commits: dict[str, list[str]],
    ) -> dict:
        """Compare planned (card) vs actual (commit aggregate).

        Returns {agreement_rate, divergence_flags, per_card}.
        """
        per_card: list[dict] = []
        agreements = 0
        total = 0

        for card_id, commit_shas in card_to_commits.items():
            card_cls = card_classifications.get(card_id)
            if not card_cls:
                continue

            # Majority vote on linked commits
            commit_cats: dict[str, int] = defaultdict(int)
            for sha in commit_shas:
                c = commit_classifications.get(sha)
                if c:
                    commit_cats[c["classification"]] += 1

            if not commit_cats:
                continue

            effective = max(commit_cats, key=commit_cats.get)
            card_intent = card_cls["classification"]
            agrees = effective == card_intent or (
                card_intent in effective or effective in card_intent
            )

            total += 1
            if agrees:
                agreements += 1

            per_card.append({
                "card_id": card_id,
                "intent": card_intent,
                "execution": effective,
                "agreement": agrees,
                "commit_distribution": dict(commit_cats),
            })

        return {
            "agreement_rate": agreements / total if total else 0.0,
            "total_linked": total,
            "agreements": agreements,
            "per_card": per_card,
        }

    async def classify_from_events(
        self,
        events: list[dict],
    ) -> dict[str, dict]:
        """Lightweight classification from Event dicts (no git diff data).

        Uses commit message + NLI only (no diff categories, no diff size).
        For use when full CommitAnalysis objects are unavailable (e.g.,
        replay from board data without git repo access).

        events: list of event dicts with action="commit.create",
                target=sha, metadata.message=commit_message

        Returns {sha: classification_result} and populates self._cache.
        """
        commit_events = [
            e for e in events
            if e.get("action") == "commit.create" and e.get("target")
        ]

        results: dict[str, dict] = {}
        uncached = []

        for e in commit_events:
            sha = str(e["target"])
            if sha in self._cache:
                results[sha] = self._cache[sha]
            else:
                uncached.append(e)

        if not uncached:
            return results

        # Canonicalize from message only (no diff metadata)
        canonicals: list[tuple[dict, CanonicalCommit, dict]] = []
        for e in uncached:
            msg = e.get("metadata", {}).get("message", "")
            canonical = canonicalize_commit(message=msg)
            det = classify_deterministic(canonical)
            canonicals.append((e, canonical, det))

        # NLI scoring for non-degenerate
        nli_results: dict[str, dict[str, dict[str, float]]] = {}
        nli_eligible = [(e, can, det) for e, can, det in canonicals if not can.degenerate]

        if nli_eligible and self.engine and self.hot_circuit.allow_request():
            pairs: list[tuple[str, str]] = []
            for e, can, det in nli_eligible:
                text = can.body
                for cat, hyp in COMMIT_HYPOTHESES.items():
                    pairs.append((text, hyp))

            scores = await self.engine.score_batch(pairs)
            if scores:
                idx = 0
                for e, can, det in nli_eligible:
                    sha = str(e["target"])
                    sha_scores: dict[str, dict[str, float]] = {}
                    for cat in COMMIT_HYPOTHESES:
                        sha_scores[cat] = scores[idx]
                        idx += 1
                    nli_results[sha] = sha_scores

        # Fuse and build results
        for e, canonical, det in canonicals:
            sha = str(e["target"])
            nli = nli_results.get(sha)

            category, confidence, details = fuse_signals(
                nli_scores=nli,
                cc_prefix=canonical.prefix,
                diff_categories=None,  # no diff data available
                keyword_match=det["keyword_result"],
                diff_size=0,
            )

            nli_contributed = nli is not None and confidence is not None

            result = {
                "sha": sha,
                "classification": category,
                "confidence": confidence,
                "source": "nli" if nli_contributed else ("degenerate" if canonical.degenerate else "heuristic"),
                "classification_deterministic": det["category"],
                "nli_contributed": nli_contributed,
                "agreement": category == det["category"],
                "scores": nli,
                "signals": details.get("signals", {}),
                "margin": None,
                "circuit_state": self.hot_circuit.state,
            }

            if nli:
                sorted_ent = sorted(
                    [s.get("entailment", 0) for s in nli.values()],
                    reverse=True,
                )
                if len(sorted_ent) >= 2:
                    result["margin"] = sorted_ent[0] - sorted_ent[1]

            results[sha] = result

            # Cache NLI-backed results
            if nli_contributed:
                self._cache[sha] = result

        return results

    @staticmethod
    def calibration_report(
        classifications: list[dict],
    ) -> dict:
        """Compare NLI vs deterministic across all cached results.

        Returns agreement_rate, nli_rescue_rate, nli_override_rate, per_category.
        """
        total = 0
        agreements = 0
        rescues = 0      # deterministic "other", NLI provided real classification
        overrides = 0    # both had opinions, disagreed

        per_category: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for cls in classifications:
            total += 1
            fused = cls.get("classification", "other")
            det = cls.get("classification_deterministic", "other")

            if fused == det:
                agreements += 1
            elif det == "other" and fused != "other":
                rescues += 1
            elif det != "other" and fused != "other" and det != fused:
                overrides += 1

            per_category[fused][f"det_said_{det}"] += 1

        return {
            "total": total,
            "agreement_rate": agreements / total if total else 0.0,
            "nli_rescue_rate": rescues / total if total else 0.0,
            "nli_override_rate": overrides / total if total else 0.0,
            "per_category": dict(per_category),
        }
