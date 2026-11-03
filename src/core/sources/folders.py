from __future__ import annotations

from pathlib import Path

from src.core.sources.base import Source
from src.utils.i18n import InputError


class FolderSource(Source):
    """A folder chosen by the user (for example an Obsidian vault), exposed to one agent as `<name>:`.

    The folder must already exist; it is never created. Hidden entries such as `.obsidian/`, `.git/` or
    `.trash/` are left out of listings, searches and the index. `readable` and `writable` follow the
    agent's folder tool groups (and `read_only`); `indexed` follows the folder's own setting.
    """

    def __init__(self, name: str, root: Path, *, readable: bool = True, writable: bool = False, indexed: bool = True):
        folder = Path(root).expanduser()
        if not folder.is_dir():
            raise InputError(
                "folder '{name}' not found: {path}; fix `folders` in the agent configuration",
                name=name, path=str(folder),
            )
        self.name = name
        self.root = folder
        self.readable = readable
        self.writable = writable
        self.indexed = indexed

    def list(self, subpath: str = "") -> list[str]:
        return [entry for entry in super().list(subpath) if not hidden(entry)]

    def walk(self, exts: set[str] | None = None) -> list[str]:
        return [entry for entry in super().walk(exts) if not hidden(entry)]

    def resolve(self, rel_path: str) -> Path:
        target = super().resolve(rel_path)
        root = self.root.resolve()
        if target != root and hidden(target.relative_to(root).as_posix()):
            raise ValueError(f"hidden entries of folder '{self.name}' are not accessible: {rel_path!r}")
        return target


def hidden(rel_path: str) -> bool:
    """Whether a relative path goes through a hidden entry (a name starting with '.')."""
    return any(part.startswith(".") for part in rel_path.rstrip("/").split("/") if part)
