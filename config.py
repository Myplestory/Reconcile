"""
Pipeline configuration — YAML-based with sensible defaults and auto-detection.

Usage:
    config = load_config("team.yaml")
    config = load_config()  # empty defaults
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class GitConfig:
    repo: str = ""
    fallback: str = ""
    bundle: str = ""

    def resolve(self, root: Path) -> None:
        if self.repo and not os.path.isabs(self.repo):
            self.repo = str(root / self.repo)
        if self.fallback and not os.path.isabs(self.fallback):
            self.fallback = str(root / self.fallback)
        if self.bundle and not os.path.isabs(self.bundle):
            self.bundle = str(root / self.bundle)

    @property
    def path(self) -> str:
        """Return the first available git source."""
        for p in [self.repo, self.fallback]:
            if p and os.path.exists(p):
                return p
        return self.repo  # let caller handle missing


@dataclass
class SourcesConfig:
    git: GitConfig = field(default_factory=GitConfig)
    board_json: str = ""
    discord_dir: str = ""
    email_dir: str = ""
    source_dir: str = ""
    snapshot_dir: str = ""

    def resolve(self, root: Path) -> None:
        self.git.resolve(root)
        for attr in ("board_json", "discord_dir", "email_dir", "source_dir", "snapshot_dir"):
            val = getattr(self, attr)
            if val and not os.path.isabs(val):
                setattr(self, attr, str(root / val))


@dataclass
class ScoringConfig:
    tier_boundaries: list[int] = field(default_factory=lambda: [3, 6, 9])
    permutation_count: int = 100
    sensitivity_variants: int = 3
    permutation_seed: int = 42


@dataclass
class PipelineConfig:
    """Complete pipeline configuration."""

    # Team metadata
    team_name: str = ""
    course: str = ""
    pm_name: str = ""

    # Data sources
    sources: SourcesConfig = field(default_factory=SourcesConfig)

    # Identity mapping (git author → canonical name)
    identity_map: dict[str, str] = field(default_factory=dict)

    # Board user ID → name mapping
    board_user_map: dict[int, str] = field(default_factory=dict)

    # Sanctioned transfers (set of file paths documented as intentional)
    sanctioned_transfers: set[str] = field(default_factory=set)

    # Board pipeline ID → name mapping
    pipeline_map: dict[str, str] = field(default_factory=dict)

    # Broad UID → name mapping (includes non-team members like instructors)
    uid_name_map: dict[int, str] = field(default_factory=dict)

    # Activity types to filter out (auto-generated, not user actions)
    noise_types: set[str] = field(default_factory=lambda: {"loaded board", "card access"})

    # Third-party code prefixes excluded from authorship analysis
    vendor_paths: tuple[str, ...] = ("node_modules/", "vendor/", ".git/")

    # Member colors for visualization
    member_colors: dict[str, str] = field(default_factory=dict)

    # Scoring
    scoring: ScoringConfig = field(default_factory=ScoringConfig)

    # Output
    output_dir: str = ""

    # Pipeline root (resolved at load time)
    root: str = ""

    def resolve_paths(self) -> None:
        root = Path(self.root) if self.root else _ROOT
        self.sources.resolve(root)
        if self.output_dir and not os.path.isabs(self.output_dir):
            self.output_dir = str(root / self.output_dir)
        # sanctioned_transfers are file paths, not filesystem paths — no resolution needed


def load_config(path: str | None = None) -> PipelineConfig:
    """Load config from YAML file, try config.yaml at project root, or return empty defaults."""
    if path and HAS_YAML:
        return _load_yaml(path)
    if path and not HAS_YAML:
        raise ImportError("PyYAML required for config files: pip install pyyaml")
    # Try default config.yaml at project root
    default_yaml = os.path.join(str(_ROOT), "config.yaml")
    if os.path.exists(default_yaml) and HAS_YAML:
        return _load_yaml(default_yaml)
    return _default_instance_config()


def _load_yaml(path: str) -> PipelineConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    config = PipelineConfig(
        team_name=raw.get("team", {}).get("name", ""),
        course=raw.get("team", {}).get("course", ""),
        pm_name=raw.get("team", {}).get("pm", ""),
        root=str(Path(path).resolve().parent),
    )

    sources = raw.get("sources", {})
    git = sources.get("git", {})
    config.sources = SourcesConfig(
        git=GitConfig(
            repo=git.get("repo", ""),
            fallback=git.get("fallback", ""),
            bundle=git.get("bundle", ""),
        ),
        board_json=sources.get("board", {}).get("activity_json", ""),
        discord_dir=sources.get("discord", {}).get("export_dir", ""),
        email_dir=sources.get("email", {}).get("archive_dir", ""),
        source_dir=sources.get("source", {}).get("js_dir", ""),
        snapshot_dir=sources.get("snapshot", {}).get("dir", ""),
    )

    config.identity_map = raw.get("identity", {})
    config.sanctioned_transfers = set(raw.get("sanctioned_transfers", []))

    scoring = raw.get("scoring", {})
    config.scoring = ScoringConfig(
        tier_boundaries=scoring.get("tier_boundaries", [3, 6, 9]),
        permutation_count=scoring.get("permutation_count", 100),
        sensitivity_variants=scoring.get("sensitivity_variants", 3),
        permutation_seed=scoring.get("permutation_seed", 42),
    )

    config.output_dir = raw.get("output_dir", "./output")
    config.resolve_paths()
    return config


def _default_instance_config() -> PipelineConfig:
    """Return empty config with generic defaults. Use --config <file.yaml> for real data."""
    config = PipelineConfig(
        root=str(_ROOT),
        output_dir="audit-output",
    )
    config.resolve_paths()
    return config
