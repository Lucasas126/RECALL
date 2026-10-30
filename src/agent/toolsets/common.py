from __future__ import annotations

from src.core.sources.base import Source
from src.core.sources.registry import SourceRegistry


def readable_sources(sources: SourceRegistry, source: str) -> list[Source]:
    """Return readable sources, optionally restricted to one source name."""
    names = [source] if source else sources.names()
    return [sources.get(name) for name in names if sources.get(name).readable]


def readonly_error(sources: SourceRegistry, memory_path: str) -> str | None:
    """Return an error when an address resolves to a read-only source."""
    source, _ = sources.resolve(memory_path)
    if not source.writable:
        return f"error: source '{source.name}' is read-only; you can only write to note sources like mind_palace"
    return None
