from __future__ import annotations

from pathlib import Path

from src.core.sources.base import Source


class MindPalaceSource(Source):
    """The agent's private, obsidian-like memory. Read/write and indexed."""

    def __init__(self, root: Path):
        super().__init__("mind_palace", root, readable=True, writable=True, indexed=True)
