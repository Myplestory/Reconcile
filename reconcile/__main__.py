"""
CLI entry point: python -m reconcile

Usage:
    python -m reconcile                              # Run full pipeline with defaults
    python -m reconcile --config myteam.yaml         # Run with custom config
    python -m reconcile --phase ingest analyze       # Run specific phases
    python -m reconcile --verify                     # Verify evidence manifest only
    python -m reconcile --snapshot                   # Capture git snapshot before analysis
"""

import argparse
import sys

from .config import load_config
from .pipeline import Reconcile


def main():
    parser = argparse.ArgumentParser(
        prog="reconcile",
        description="Reconcile — Multi-source record reconciliation for software team activity.",
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to YAML config file. If omitted, uses built-in defaults.",
    )
    parser.add_argument(
        "--phase", "-p",
        nargs="+",
        choices=["ingest", "analyze", "forensics", "output"],
        help="Run specific phases only (default: all).",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify evidence manifest and exit.",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="Capture git snapshot before analysis.",
    )
    parser.add_argument(
        "--git-repo",
        help="Path to git repository (overrides config).",
    )
    parser.add_argument(
        "--board-json",
        help="Path to board activity JSON (overrides config).",
    )
    parser.add_argument(
        "--discord-dir",
        help="Path to Discord export directory (overrides config).",
    )
    parser.add_argument(
        "--email-dir",
        help="Path to status report email directory (overrides config).",
    )
    parser.add_argument(
        "--output-dir", "-o",
        help="Output directory (overrides config).",
    )
    parser.add_argument(
        "--pm",
        help="PM name (overrides config).",
    )

    args = parser.parse_args()

    # Load config
    config = load_config(args.config)

    # CLI overrides
    if args.git_repo:
        config.sources.git.repo = args.git_repo
    if args.board_json:
        config.sources.board_json = args.board_json
    if args.discord_dir:
        config.sources.discord_dir = args.discord_dir
    if args.email_dir:
        config.sources.email_dir = args.email_dir
    if args.output_dir:
        config.output_dir = args.output_dir
    if args.pm:
        config.pm_name = args.pm
    if args.snapshot:
        if not config.sources.snapshot_dir:
            config.sources.snapshot_dir = "data/snapshots"

    # Verify-only mode
    if args.verify:
        from .forensics import manifest
        ok = manifest.verify(config)
        sys.exit(0 if ok else 1)

    # Run pipeline
    pipeline = Reconcile(config=config)
    pipeline.run(phases=args.phase)


if __name__ == "__main__":
    main()
