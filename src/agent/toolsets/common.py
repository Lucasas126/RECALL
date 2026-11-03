from __future__ import annotations

from src.core.sources.base import Source
from src.core.sources.registry import SourceRegistry


def readable_sources(sources: SourceRegistry, source: str) -> list[Source]:
    """Return readable sources, optionally restricted to one source name."""
    names = [source] if source else sources.names()
    return [sources.get(name) for name in names if sources.get(name).readable]


def mark_written(agent, address: str) -> None:
    """Tell the agent a tool changed `address`, so the index picks it up after the turn."""
    note = getattr(agent, "note_written", None)
    if note is not None:
        note(address)


def readonly_error(sources: SourceRegistry, memory_path: str) -> str | None:
    """Return an error when an address resolves to a read-only source."""
    source, _ = sources.resolve(memory_path)
    if not source.writable:
        return f"error: source '{source.name}' is read-only here; write to mind_palace or to a folder you may edit"
    return None
