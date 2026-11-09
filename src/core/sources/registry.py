from __future__ import annotations

from pathlib import Path
from typing import Sequence

from src.config.models import FolderConfig
from src.core.sources.agent_files import AgentFilesSource
from src.core.sources.base import Source
from src.config.settings import DATA_DIR
from src.core.sources.conversations import ConversationsSource
from src.core.sources.dream import DreamStateSource
from src.core.sources.folders import FolderSource, folder_path_problem
from src.core.sources.mind_palace import MindPalaceSource
from src.utils.logger import get_logger

logger = get_logger("sources")


class SourceRegistry:
    """Per-agent access point to the data plane: address resolution + typed accessors."""

    def __init__(self, sources: dict[str, Source], *, unavailable_folders: frozenset[str] = frozenset()):
        self._sources = sources
        # configured folders that are temporarily missing: their indexed chunks are kept, unlike removed folders
        self.unavailable_folders = unavailable_folders

    @classmethod
    def for_agent(
        cls, user_id: str, agent_id: str, *, folders: Sequence[FolderConfig] = (),
        folders_readable: bool = False, folders_writable: bool = False, data_dir: Path | None = None,
    ) -> "SourceRegistry":
        """Build an agent's sources; each of `folders` becomes a source named after it.

        The folders are always registered (so indexed ones feed the index); the tools can read them only
        when `folders_readable` and write them only when `folders_writable` and the folder is not read-only.
        """
        base = (data_dir or DATA_DIR) / user_id / agent_id
        sources: dict[str, Source] = {
            "mind_palace": MindPalaceSource(base / "mind_palace"),
            "conversations": ConversationsSource(base / "conversations"),
            "agent": AgentFilesSource(base),
            "dream": DreamStateSource(base / "dream"),
        }
        unavailable: set[str] = set()
        for folder in folders:
            if folder.name in sources:
                raise ValueError(f"folder name {folder.name!r} clashes with another source")
            if not Path(folder.path).expanduser().is_dir():
                # an unmounted drive or a moved vault must not stop the agent: skip the folder for now
                # (saving the agent still rejects a missing path); its indexed chunks are kept
                logger.warning(
                    "folder %r of %s/%s not found at %s; skipped until it is available",
                    folder.name, user_id, agent_id, folder.path,
                )
                unavailable.add(folder.name)
                continue
            if problem := folder_path_problem(folder.path):
                logger.warning(
                    "folder %r of %s/%s at %s skipped: %s", folder.name, user_id, agent_id, folder.path, problem,
                )
                continue
            sources[folder.name] = FolderSource(
                folder.name, Path(folder.path), readable=folders_readable or folders_writable,
                writable=folders_writable and not folder.read_only, indexed=folder.indexed,
            )
        logger.debug("registry for %s/%s -> %s", user_id, agent_id, list(sources))
        return cls(sources, unavailable_folders=frozenset(unavailable))

    # ------------------------------------------------------------------ access

    def get(self, name: str) -> Source:
        if name not in self._sources:
            raise KeyError(f"unknown source: {name!r}")
        return self._sources[name]

    def names(self) -> list[str]:
        """Names of the sources the agent's tools can read."""
        return [name for name, source in self._sources.items() if source.readable]

    def source_names(self) -> list[str]:
        """Every registered source, readable or not."""
        return list(self._sources)

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

    def folders(self) -> list[FolderSource]:
        """The user folders exposed to this agent, in configuration order."""
        return [source for source in self._sources.values() if isinstance(source, FolderSource)]
