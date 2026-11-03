from __future__ import annotations

from pathlib import Path

from src.core.sources.agent_files import AgentFilesSource
from src.core.sources.base import Source
from src.config.settings import DATA_DIR
from src.core.sources.conversations import ConversationsSource
from src.core.sources.dream import DreamStateSource
from src.core.sources.mind_palace import MindPalaceSource
from src.core.sources.vault import VaultSource
from src.utils.logger import get_logger

logger = get_logger("sources")


class SourceRegistry:
    """Per-agent access point to the data plane: address resolution + typed accessors."""

    def __init__(self, sources: dict[str, Source]):
        self._sources = sources

    @classmethod
    def for_agent(
        cls, user_id: str, agent_id: str, *, vault: str | None = None, vault_writable: bool = False,
        data_dir: Path | None = None,
    ) -> "SourceRegistry":
        """Build an agent's sources; `vault` adds the user's folder as the `vault` source."""
        base = (data_dir or DATA_DIR) / user_id / agent_id
        sources: dict[str, Source] = {
            "mind_palace": MindPalaceSource(base / "mind_palace"),
            "conversations": ConversationsSource(base / "conversations"),
            "agent": AgentFilesSource(base),
            "dream": DreamStateSource(base / "dream"),
        }
        if vault is not None:
            sources["vault"] = VaultSource(Path(vault), writable=vault_writable)
        logger.debug("registry for %s/%s -> %s", user_id, agent_id, list(sources))
        return cls(sources)

    # ------------------------------------------------------------------ access

    def get(self, name: str) -> Source:
        if name not in self._sources:
            raise KeyError(f"unknown source: {name!r}")
        return self._sources[name]

    def names(self) -> list[str]:
        """Names of the sources the agent's tools can read."""
        return [name for name, source in self._sources.items() if source.readable]

    def has(self, name: str) -> bool:
        return name in self._sources

    def indexed_sources(self) -> list[Source]:
        return [s for s in self._sources.values() if s.indexed]

    def resolve(self, address: str) -> tuple[Source, str]:
        """Split a 'source:path' address into its source and relative path."""
        if ":" not in address:
            raise ValueError(f"invalid address (expected 'source:path'): {address!r}")
        name, rel = address.split(":", 1)
        return self.get(name), rel

    # ------------------------------------------------------------------ ops

    def read(self, address: str) -> str:
        source, rel = self.resolve(address)
        return source.read(rel)

    def write(self, address: str, content: str) -> Path:
        source, rel = self.resolve(address)
        return source.write(rel, content)

    def delete(self, address: str) -> None:
        source, rel = self.resolve(address)
        source.delete(rel)

    def list(self, name: str = "", subpath: str = "") -> list[str]:
        """List readable sources (no name) or the entries inside one source."""
        if not name:
            return self.names()
        return self.get(name).list(subpath)

    # ------------------------------------------------------------------ typed accessors

    def mind_palace(self) -> Source:
        return self.get("mind_palace")

    def conversations(self) -> ConversationsSource:
        return self.get("conversations")

    def agent_files(self) -> AgentFilesSource:
        return self.get("agent")

    def dream_state(self) -> DreamStateSource:
        return self.get("dream")

    def vault(self) -> VaultSource | None:
        return self._sources.get("vault")
