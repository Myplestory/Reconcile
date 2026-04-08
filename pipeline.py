"""
Batch pipeline runner — one-shot analysis of existing data.

This is the batch counterpart to the real-time Orchestrator.
Orchestrator runs live (WebSocket, polling, dashboard).
Reconcile runs once (load files, analyze, write output, exit).

Usage:
    from reconcile.pipeline import Reconcile
    from reconcile.config import load_config

    pipeline = Reconcile(config=load_config("team.yaml"))
    state = pipeline.run()
"""

from __future__ import annotations

import os
import sys
import time

from .config import PipelineConfig, load_config
from .normalize.types import PipelineState
from .normalize.timeline import Timeline


class Reconcile:
    """Batch pipeline. Instantiate with config, call run()."""

    def __init__(self, config: PipelineConfig | None = None, config_path: str | None = None):
        if config is not None:
            self.config = config
        else:
            self.config = load_config(config_path)
        self.state = PipelineState()
        self.timeline = Timeline()
        self._phase_times: dict[str, float] = {}

    def run(self, phases: list[str] | None = None) -> PipelineState:
        """Run all or selected phases. Returns final pipeline state."""
        phases = phases or ["ingest", "analyze", "forensics", "output"]

        self._print_header()

        for phase_name in phases:
            method = getattr(self, f"phase_{phase_name}", None)
            if method is None:
                print(f"  Unknown phase: {phase_name}", file=sys.stderr)
                continue
            t0 = time.time()
            try:
                method()
                self._phase_times[phase_name] = time.time() - t0
            except Exception as e:
                print(f"\n  FAILED in phase '{phase_name}': {e}", file=sys.stderr)
                raise

        self._print_summary()
        return self.state

    # ── Phase implementations ──

    def phase_ingest(self):
        """Phase 0: Collect and normalize all sources into PipelineState."""
        self._print_phase("Ingest & Normalize")

        from .ingest import git, board, discord, email

        # Git
        self.state.commits, self.state.branches, self.state.files = git.load(self.config)
        if hasattr(git.load, "_raw"):
            self.state.raw_artifacts.update(git.load._raw)
        self._tick(f"Git: {len(self.state.commits)} commits, {len(self.state.branches)} branches, {len(self.state.files)} files")

        # Board
        self.state.board_events, self.state.cards = board.load(self.config)
        if hasattr(board.load, "_raw"):
            self.state.raw_artifacts.update(board.load._raw)
        self._tick(f"Board: {len(self.state.board_events)} events, {len(self.state.cards)} cards")

        # Discord
        self.state.messages = discord.load(self.config)
        self._tick(f"Discord: {len(self.state.messages)} messages")

        # Email
        self.state.reports = email.load(self.config)
        self._tick(f"Email: {len(self.state.reports)} reports")

        # Build unified timeline
        all_events = (
            [c.to_event() for c in self.state.commits]
            + self.state.board_events
            + [m.to_event() for m in self.state.messages]
            + [r.to_event() for r in self.state.reports]
        )
        self.timeline.build(all_events)
        self.state.timeline = list(self.timeline)
        self._tick(f"Unified timeline: {len(self.timeline)} events")

    def phase_analyze(self):
        """Phase 1: Cross-reference, invariant checks, scoring."""
        self._print_phase("Cross-Reference & Analyze")

        from .analyze import invariants, dag, provenance, scoring, pairs, discord

        # DAG
        self.state.dag = dag.build(self.state.commits, self.config)
        self._tick("Commit DAG built")

        # Provenance
        provenance.compute._raw_artifacts = self.state.raw_artifacts
        self.state.provenance = provenance.compute(
            self.state.dag, self.state.branches, self.state.board_events, self.config
        )
        self._tick("Branch provenance resolved")

        # Invariant checks
        self.state.observations = invariants.check_all(
            self.state.commits, self.state.branches, self.state.cards,
            self.state.files, self.state.board_events, self.config,
            raw_artifacts=self.state.raw_artifacts,
        )
        self._tick(f"Invariant checks: {len(self.state.observations)} observations")

        # Scoring
        self.state.scores = scoring.compute(
            self.state.observations, self.state.dag,
            self.state.provenance, self.config
        )
        self._tick("Scoring complete")

        # Pair analysis
        raw_violations = getattr(invariants.check_all, "_raw_violations", [])
        pairs.compute._raw_violations = raw_violations
        pairs.compute._pm_name = self.config.pm_name
        self.state.pairs = pairs.compute(self.state.scores, self.state.board_events)
        self._tick("Pair analysis complete")

        # Discord content classification
        self.state.classifications = discord.classify(self.state.messages, self.config)
        self._tick(f"Content classification: {len(self.state.messages)} messages")

    def phase_forensics(self):
        """Phase 2: Verify evidence integrity."""
        self._print_phase("Forensic Verification")

        from .forensics import snowflake, smtp, consent, manifest

        # Snowflake validation
        self.state.snowflake_validation = snowflake.validate(self.state.messages)
        total = self.state.snowflake_validation.get("total", 0)
        anomalies = self.state.snowflake_validation.get("anomalies", 0)
        self._tick(f"Snowflake: {total} validated, {anomalies} anomalies")

        # SMTP analysis
        self.state.email_analysis = smtp.analyze(self.state.reports)
        self._tick(f"SMTP concordance: {len(self.state.reports)} emails")

        # Consent search
        self.state.consent_results = consent.search(
            self.state.observations, self.state.messages
        )
        self._tick("Digital consent search complete")

        # Evidence manifest
        self.state.manifest = manifest.generate(self.config)
        self._tick("Evidence manifest generated")

    def phase_output(self):
        """Phase 3: Generate all output artifacts."""
        self._print_phase("Output")

        from .output import json_artifacts, markdown

        os.makedirs(self.config.output_dir, exist_ok=True)

        json_artifacts.write(self.state, self.config)
        self._tick("JSON artifacts written")

        markdown.write(self.state, self.config)
        self._tick("Markdown reports written")

        self._tick(f"All outputs -> {self.config.output_dir}/")

    # ── Display helpers ──

    def _print_header(self):
        print()
        print("Reconcile -- Batch Pipeline")
        print("-" * 48)
        if self.config.team_name:
            print(f"  Team: {self.config.team_name}")
        if self.config.course:
            print(f"  Course: {self.config.course}")
        print()

    def _print_phase(self, name: str):
        print(f"Phase: {name}")

    def _tick(self, msg: str):
        print(f"  + {msg}")

    def _print_summary(self):
        print()
        print("-" * 48)
        total = sum(self._phase_times.values())
        for phase, elapsed in self._phase_times.items():
            print(f"  {phase:<20} {elapsed:.1f}s")
        print(f"  {'total':<20} {total:.1f}s")
        print()
        if self.config.output_dir:
            print(f"  Output: {self.config.output_dir}/")
        print()
