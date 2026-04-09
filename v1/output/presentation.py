"""Cross-reference findings document generation."""
from __future__ import annotations
from ..normalize.types import PipelineState
from ..config import PipelineConfig

def write(state: PipelineState, config: PipelineConfig) -> None:
    """Write presentation.md."""
    # TODO: Extract from cross_reference_discord.py generate_report()
    pass
