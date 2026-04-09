"""SHA-256 evidence manifest — chain of custody for all artifacts."""

from __future__ import annotations

import glob
import hashlib
import json
import os
import subprocess
from datetime import datetime

from ..config import PipelineConfig


def _sha256(filepath: str) -> str | None:
    """Compute SHA-256 hash of a file."""
    try:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None


def _git_revision(root: str) -> str:
    """Get current git HEAD revision."""
    try:
        r = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return r.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


def generate(config: PipelineConfig) -> dict:
    """Generate SHA-256 hashes for all artifacts in the output directory.

    Unlike the legacy manifest which had hardcoded file lists, this
    dynamically hashes everything in the output directory.
    """
    root = config.root or "."
    out = config.output_dir

    manifest: dict[str, dict] = {
        "generated": datetime.now().isoformat(),
        "git_revision": _git_revision(root),
        "artifacts": {},
    }

    if out and os.path.isdir(out):
        for fpath in sorted(glob.glob(os.path.join(out, "*"))):
            if os.path.isfile(fpath):
                rel = os.path.relpath(fpath, root)
                h = _sha256(fpath)
                manifest["artifacts"][rel] = h or "MISSING"

    return manifest


def verify(config: PipelineConfig) -> bool:
    """Verify existing manifest against current files.

    Returns True if all hashes match.
    """
    manifest_path = os.path.join(config.output_dir, "evidence-manifest.json")
    if not os.path.exists(manifest_path):
        print("No manifest found.")
        return False

    with open(manifest_path) as f:
        manifest = json.load(f)

    root = config.root or "."
    all_ok = True
    artifacts = manifest.get("artifacts", {})

    for rel_path, expected_hash in artifacts.items():
        full_path = os.path.join(root, rel_path)
        actual = _sha256(full_path)
        if actual is None:
            print(f"  MISSING: {rel_path}")
            all_ok = False
        elif actual != expected_hash:
            print(f"  CHANGED: {rel_path}")
            all_ok = False

    if all_ok:
        print(f"  All {len(artifacts)} artifacts verified.")

    return all_ok
