"""Tests for reconcile.analyze.commit_classifier — NLI + deterministic classification.

Covers:
  - CircuitBreaker state transitions
  - Canonicalization (CC prefix, file stripping, degenerate detection)
  - Deterministic classification (keyword, CC map, diff categories)
  - Multi-signal fusion (weight redistribution, all-signals, deterministic-only)
  - CommitClassifier batch classification (mock engine, cache, fallback)
  - Card classification (deterministic, heuristic-weak)
  - Calibration report computation
  - Cross-reference (intent vs execution alignment)
"""

from __future__ import annotations

import asyncio
import time

import pytest

from reconcile.analyze.commit_classifier import (
    CircuitBreaker,
    CanonicalCommit,
    CommitClassifier,
    CC_MAP,
    COMMIT_HYPOTHESES,
    CARD_HYPOTHESES,
    canonicalize_commit,
    canonicalize_card,
    classify_deterministic,
    classify_card_deterministic,
    fuse_signals,
)
from reconcile.analyze.code_quality import CommitAnalysis, FileChange


# ============================================================
# CircuitBreaker
# ============================================================


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker()
        assert cb.state == CircuitBreaker.CLOSED
        assert cb.allow_request() is True

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        cb.record_failure()
        assert cb.state == CircuitBreaker.CLOSED
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.allow_request() is False

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        assert cb._failure_count == 0
        cb.record_failure()
        cb.record_failure()
        # Should still be closed — count was reset
        assert cb.state == CircuitBreaker.CLOSED

    def test_half_open_after_recovery_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN
        time.sleep(0.02)
        assert cb.allow_request() is True
        assert cb.state == CircuitBreaker.HALF_OPEN

    def test_half_open_to_closed_after_successes(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01, success_threshold=2)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()  # transitions to HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitBreaker.HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitBreaker.CLOSED

    def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()
        assert cb.state == CircuitBreaker.HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitBreaker.OPEN

    def test_force_open(self):
        cb = CircuitBreaker()
        cb.force_open()
        assert cb.state == CircuitBreaker.OPEN
        assert cb.allow_request() is False


# ============================================================
# Canonicalization
# ============================================================


class TestCanonicalizeCommit:
    def test_conventional_commit_prefix(self):
        c = canonicalize_commit("feat(auth): add password reset")
        assert c.prefix == "feat"
        assert c.scope == "auth"
        assert c.body == "add password reset"
        assert c.degenerate is False

    def test_fix_prefix(self):
        c = canonicalize_commit("fix: resolve login bug")
        assert c.prefix == "fix"
        assert c.scope is None
        assert "resolve login bug" in c.body

    def test_strip_file_references(self):
        c = canonicalize_commit("drivers.php, patch endpoint added for orders.php")
        assert "drivers.php" not in c.body
        assert "orders.php" not in c.body
        assert "patch endpoint added" in c.body

    def test_degenerate_short_message(self):
        c = canonicalize_commit("map")
        assert c.degenerate is True

    def test_degenerate_github_default(self):
        c = canonicalize_commit("Add files via upload")
        assert c.degenerate is True

    def test_degenerate_update_pattern(self):
        c = canonicalize_commit("Update README.md")
        assert c.degenerate is True

    def test_normal_message_not_degenerate(self):
        c = canonicalize_commit("reconciled with shuning map changes for frontend")
        assert c.degenerate is False

    def test_enrichment_with_diff_metadata(self):
        c = canonicalize_commit(
            "added new login page",
            diff_categories=["frontend:page", "backend:api"],
            total_adds=45,
            total_dels=12,
            file_count=3,
        )
        assert "Modified 3 file(s)" in c.enrichment
        assert "frontend:page" in c.enrichment

    def test_no_enrichment_without_metadata(self):
        c = canonicalize_commit("some commit message")
        assert c.enrichment == ""

    def test_raw_preserved(self):
        c = canonicalize_commit("  feat: trimmed  ")
        assert c.raw == "feat: trimmed"


class TestCanonicalizeCard:
    def test_normal_card(self):
        c = canonicalize_card("Implement login page with OAuth integration")
        assert c.degenerate is False
        assert "login page" in c.body

    def test_degenerate_short_card(self):
        c = canonicalize_card("Map")
        assert c.degenerate is True

    def test_strip_user_story_prefix(self):
        c = canonicalize_card("Shihao Liu - User story 1 something about login")
        # The prefix "Shihao Liu - User story 1" should be stripped
        assert "User story" not in c.body

    def test_strip_frontend_backend_suffix(self):
        c = canonicalize_card("Login page implementation (frontend)")
        assert "(frontend)" not in c.body
        assert "Login page" in c.body

    def test_append_comment_for_short_title(self):
        c = canonicalize_card("Fix bug", comments=["The login form crashes on submit"])
        assert "login form" in c.body


# ============================================================
# Deterministic Classification
# ============================================================


class TestDeterministic:
    def test_cc_prefix_takes_priority(self):
        canonical = canonicalize_commit("feat: add dashboard")
        result = classify_deterministic(canonical)
        assert result["category"] == "feature"
        assert result["cc_result"] == "feature"

    def test_keyword_bugfix(self):
        canonical = canonicalize_commit("fix login crash on submit")
        result = classify_deterministic(canonical)
        assert result["category"] == "maintenance:bugfix"
        assert result["keyword_result"] == "maintenance:bugfix"

    def test_keyword_refactor(self):
        canonical = canonicalize_commit("refactored shared modules into service layer")
        result = classify_deterministic(canonical)
        assert result["category"] == "maintenance:refactor"

    def test_keyword_feature(self):
        canonical = canonicalize_commit("add new endpoint for order tracking")
        result = classify_deterministic(canonical)
        assert result["category"] == "feature"

    def test_diff_categories_fallback(self):
        canonical = canonicalize_commit("reconciled with shuning map changes")
        result = classify_deterministic(
            canonical,
            diff_categories=["frontend:page", "frontend:component"],
        )
        # No keyword match, so diff categories drive result
        assert result["diff_result"] is not None

    def test_small_diff_bugfix_heuristic(self):
        canonical = canonicalize_commit("line 28 same approach")
        result = classify_deterministic(canonical, diff_size=10)
        assert result["category"] == "maintenance:bugfix"

    def test_no_signal_returns_other(self):
        canonical = canonicalize_commit("updated something somewhere")
        # degenerate but no CC, no keyword match, no diff
        result = classify_deterministic(canonical)
        # "updated" doesn't match our keyword patterns precisely
        assert result["category"] in ("other", "maintenance:bugfix", "feature")

    def test_cc_map_coverage(self):
        """All CC prefixes should map to valid categories."""
        for prefix, category in CC_MAP.items():
            assert category in COMMIT_HYPOTHESES or category.split(":")[0] in (
                "feature", "maintenance", "test", "devops", "other",
            ), f"CC prefix '{prefix}' maps to unmapped category '{category}'"


class TestCardDeterministic:
    def test_feature_keyword(self):
        canonical = canonicalize_card("Implement new dashboard page")
        result = classify_card_deterministic(canonical)
        assert result["category"] == "feature"
        assert result["source"] == "heuristic-weak"

    def test_bugfix_keyword(self):
        canonical = canonicalize_card("Fix broken login form")
        result = classify_card_deterministic(canonical)
        assert result["category"] == "bugfix"

    def test_research_keyword(self):
        canonical = canonicalize_card("Research map API feasibility")
        result = classify_card_deterministic(canonical)
        assert result["category"] == "research"

    def test_test_keyword(self):
        canonical = canonicalize_card("Write unit tests for API endpoints")
        result = classify_card_deterministic(canonical)
        assert result["category"] == "test"

    def test_pipeline_fallback(self):
        canonical = canonicalize_card("some vague card title that has enough words")
        result = classify_card_deterministic(canonical, pipeline_name="Testing")
        assert result["category"] == "test"

    def test_default_is_feature(self):
        canonical = canonicalize_card("something with enough words for classification")
        result = classify_card_deterministic(canonical)
        assert result["category"] == "feature"


# ============================================================
# Fusion
# ============================================================


class TestFusion:
    def test_nli_confident_wins(self):
        nli_scores = {
            "feature": {"entailment": 0.85, "neutral": 0.10, "contradiction": 0.05},
            "maintenance:bugfix": {"entailment": 0.10, "neutral": 0.70, "contradiction": 0.20},
        }
        category, confidence, details = fuse_signals(
            nli_scores=nli_scores,
            cc_prefix=None,
            diff_categories=None,
            keyword_match=None,
            diff_size=50,
        )
        assert category == "feature"
        assert confidence is not None
        assert confidence > 0.8

    def test_nli_not_confident_fallback(self):
        # NLI margin too thin
        nli_scores = {
            "feature": {"entailment": 0.35, "neutral": 0.35, "contradiction": 0.30},
            "maintenance:bugfix": {"entailment": 0.30, "neutral": 0.40, "contradiction": 0.30},
        }
        category, confidence, details = fuse_signals(
            nli_scores=nli_scores,
            cc_prefix=None,
            diff_categories=None,
            keyword_match="maintenance:bugfix",
            diff_size=15,
        )
        # NLI not confident, so keyword should drive
        assert confidence is None
        assert "maintenance:bugfix" in category or category == "maintenance:bugfix"

    def test_cc_prefix_authority(self):
        category, confidence, details = fuse_signals(
            nli_scores=None,
            cc_prefix="fix",
            diff_categories=None,
            keyword_match=None,
            diff_size=0,
        )
        assert category == "maintenance:bugfix"

    def test_pure_deterministic(self):
        """No NLI, no CC prefix — keyword + diff only."""
        category, confidence, details = fuse_signals(
            nli_scores=None,
            cc_prefix=None,
            diff_categories=["frontend:page", "frontend:component"],
            keyword_match="feature",
            diff_size=100,
        )
        assert confidence is None
        assert category is not None

    def test_no_signals_returns_other(self):
        category, confidence, details = fuse_signals(
            nli_scores=None,
            cc_prefix=None,
            diff_categories=None,
            keyword_match=None,
            diff_size=0,
        )
        assert category == "other"

    def test_weight_redistribution(self):
        """With only keyword + diff_size, weights should redistribute."""
        _, _, details = fuse_signals(
            nli_scores=None,
            cc_prefix=None,
            diff_categories=None,
            keyword_match="feature",
            diff_size=50,
        )
        weights = details.get("weights_used", {})
        assert "nli" not in weights
        assert "cc_prefix" not in weights
        # Remaining weights should sum to ~1.0
        assert abs(sum(weights.values()) - 1.0) < 0.01


# ============================================================
# CommitClassifier Batch
# ============================================================


def _make_commit(sha: str, message: str, files: list[str] | None = None) -> CommitAnalysis:
    """Helper to create CommitAnalysis for tests."""
    file_changes = [FileChange(path=f) for f in (files or [])]
    return CommitAnalysis(
        sha=sha, author="alice", date="2026-01-15",
        message=message, files=file_changes,
    )


class TestClassifyBatch:
    @pytest.mark.asyncio
    async def test_deterministic_without_engine(self):
        classifier = CommitClassifier(engine=None)
        commits = [
            _make_commit("abc123", "fix: resolve login bug"),
            _make_commit("def456", "feat: add dashboard"),
            _make_commit("ghi789", "map"),
        ]
        results = await classifier.classify_batch(commits)
        assert len(results) == 3
        assert results["abc123"]["classification"] == "maintenance:bugfix"
        assert results["def456"]["classification"] == "feature"
        # Degenerate — still gets a classification
        assert results["ghi789"]["source"] == "degenerate"
        assert results["ghi789"]["classification"] is not None

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        classifier = CommitClassifier(engine=None)
        commits = [_make_commit("abc123", "fix: resolve login bug")]
        # First call
        r1 = await classifier.classify_batch(commits)
        # Second call — should hit cache
        r2 = await classifier.classify_batch(commits)
        assert r1["abc123"]["classification"] == r2["abc123"]["classification"]

    @pytest.mark.asyncio
    async def test_nli_contributed_false_without_engine(self):
        classifier = CommitClassifier(engine=None)
        commits = [_make_commit("abc123", "refactored the authentication module")]
        results = await classifier.classify_batch(commits)
        assert results["abc123"]["nli_contributed"] is False

    @pytest.mark.asyncio
    async def test_all_commits_get_classification(self):
        """Every commit gets a classification — never 'unknown' or empty."""
        classifier = CommitClassifier(engine=None)
        commits = [
            _make_commit("a", "fix login"),
            _make_commit("b", ""),
            _make_commit("c", "asdf qwerty"),
            _make_commit("d", "refactor: extract shared utils"),
            _make_commit("e", "reconciled with shuning map changes for the frontend page"),
        ]
        results = await classifier.classify_batch(commits)
        for sha in ["a", "b", "c", "d", "e"]:
            assert sha in results
            assert results[sha]["classification"] is not None
            assert results[sha]["classification"] != ""


# ============================================================
# Card Classification
# ============================================================


class TestClassifyCards:
    @pytest.mark.asyncio
    async def test_card_classification(self):
        classifier = CommitClassifier(engine=None)
        cards = [
            {"card_id": "1", "title": "Implement login page with OAuth"},
            {"card_id": "2", "title": "Fix broken search results"},
            {"card_id": "3", "title": "Research map API feasibility study"},
        ]
        results = await classifier.classify_cards(cards)
        assert results["1"]["classification"] == "feature"
        assert results["2"]["classification"] == "bugfix"
        assert results["3"]["classification"] == "research"

    @pytest.mark.asyncio
    async def test_card_all_get_classification(self):
        classifier = CommitClassifier(engine=None)
        cards = [
            {"card_id": "1", "title": "x"},
            {"card_id": "2", "title": "some normal task to implement with words"},
        ]
        results = await classifier.classify_cards(cards)
        for card_id in ["1", "2"]:
            assert card_id in results
            assert results[card_id]["classification"] is not None


# ============================================================
# Cross-Reference
# ============================================================


class TestCrossReference:
    def test_agreement(self):
        card_cls = {"c1": {"classification": "feature"}}
        commit_cls = {
            "sha1": {"classification": "feature"},
            "sha2": {"classification": "feature"},
        }
        linkage = {"c1": ["sha1", "sha2"]}
        result = CommitClassifier.cross_reference(card_cls, commit_cls, linkage)
        assert result["agreement_rate"] == 1.0

    def test_divergence(self):
        card_cls = {"c1": {"classification": "feature"}}
        commit_cls = {
            "sha1": {"classification": "maintenance:bugfix"},
            "sha2": {"classification": "maintenance:bugfix"},
        }
        linkage = {"c1": ["sha1", "sha2"]}
        result = CommitClassifier.cross_reference(card_cls, commit_cls, linkage)
        assert result["agreement_rate"] == 0.0
        assert result["total_linked"] == 1

    def test_empty_linkage(self):
        result = CommitClassifier.cross_reference({}, {}, {})
        assert result["agreement_rate"] == 0.0
        assert result["total_linked"] == 0


# ============================================================
# Calibration Report
# ============================================================


class TestCalibrationReport:
    def test_all_agree(self):
        classifications = [
            {"classification": "feature", "classification_deterministic": "feature"},
            {"classification": "feature", "classification_deterministic": "feature"},
        ]
        report = CommitClassifier.calibration_report(classifications)
        assert report["agreement_rate"] == 1.0
        assert report["nli_rescue_rate"] == 0.0
        assert report["nli_override_rate"] == 0.0

    def test_rescue(self):
        classifications = [
            {"classification": "feature", "classification_deterministic": "other"},
        ]
        report = CommitClassifier.calibration_report(classifications)
        assert report["nli_rescue_rate"] == 1.0

    def test_override(self):
        classifications = [
            {"classification": "feature", "classification_deterministic": "maintenance:bugfix"},
        ]
        report = CommitClassifier.calibration_report(classifications)
        assert report["nli_override_rate"] == 1.0

    def test_empty(self):
        report = CommitClassifier.calibration_report([])
        assert report["total"] == 0
        assert report["agreement_rate"] == 0.0


# ============================================================
# Hypothesis Constants
# ============================================================


class TestHypotheses:
    def test_commit_hypothesis_count(self):
        assert len(COMMIT_HYPOTHESES) == 8

    def test_card_hypothesis_count(self):
        assert len(CARD_HYPOTHESES) == 8

    def test_commit_hypotheses_are_declarative(self):
        for cat, hyp in COMMIT_HYPOTHESES.items():
            assert hyp.startswith("This change"), f"{cat} hypothesis doesn't start with 'This change'"
            assert hyp.endswith("."), f"{cat} hypothesis doesn't end with period"

    def test_card_hypotheses_are_declarative(self):
        for cat, hyp in CARD_HYPOTHESES.items():
            assert hyp.startswith("This task"), f"{cat} hypothesis doesn't start with 'This task'"
            assert hyp.endswith("."), f"{cat} hypothesis doesn't end with period"

    def test_research_is_card_only(self):
        assert "research" in CARD_HYPOTHESES
        assert "research" not in COMMIT_HYPOTHESES
