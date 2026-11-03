from __future__ import annotations

from pathlib import Path

from src.core.sources.base import Source
from src.utils.i18n import InputError


class VaultSource(Source):
    """A folder chosen by the user (usually an Obsidian vault), exposed to one agent.

    The folder must already exist; it is never created. Hidden entries such as `.obsidian/`,
    `.git/` or `.trash/` are left out of listings and searches. Writable only when the agent's
    chat enables the `vault_edit` tool group; dreams always open it read-only.
    """

    def __init__(self, root: Path, *, writable: bool = False):
        folder = Path(root).expanduser()
        if not folder.is_dir():
            raise InputError(
                "vault folder not found: {path}; fix `vault` in the agent configuration", path=str(folder),
            )
        self.name = "vault"
        self.root = folder
        self.readable = True
        self.writable = writable
        self.indexed = False

    def list(self, subpath: str = "") -> list[str]:
        return [entry for entry in super().list(subpath) if not _hidden(entry)]

    def walk(self, exts: set[str] | None = None) -> list[str]:
        return [entry for entry in super().walk(exts) if not _hidden(entry)]


def _hidden(rel_path: str) -> bool:
    return any(part.startswith(".") for part in rel_path.rstrip("/").split("/"))
