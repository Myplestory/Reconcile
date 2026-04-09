"""Git snapshot — forensic preservation and ref scrub detection."""
from __future__ import annotations
from ..config import PipelineConfig
from ..normalize.types import SnapshotDiff

def capture_if_needed(config: PipelineConfig) -> SnapshotDiff | None:
    """Capture git bundle if no snapshot exists, or compare against existing."""
    # TODO: Implement GitSnapshot.capture(), .compare(), .restore()
    return None
