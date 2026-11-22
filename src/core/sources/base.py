from __future__ import annotations

import os
from pathlib import Path

from src.utils.files import atomic_replace_bytes, atomic_replace_text


def scan_files(root: Path, exts: set[str] | None = None, base: Path | None = None) -> dict[str, tuple[int, int]]:
    """Map each non-hidden file under `root` to its `(mtime_ns, size)`, keyed by its path relative to `base`.

    Hidden entries (names starting with '.') are skipped and never descended into; the stat data comes from the
    directory listing itself, so unchanged files are never opened.
    """
    base = base or root
    found: dict[str, tuple[int, int]] = {}
    pending = [root]
    while pending:
        folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if not entry.is_file():
                    continue
                if exts is not None and Path(entry.name).suffix.lstrip(".").lower() not in exts:
                    continue
                info = entry.stat()
            except OSError:
                continue
            found[Path(entry.path).relative_to(base).as_posix()] = (info.st_mtime_ns, info.st_size)
    return found


def pdf_text(data: bytes, name: str) -> str:
    """The digital text of a PDF with a `[Page N]` marker per page (no OCR); tools read PDFs the agent or the user saved."""
    import pymupdf

    try:
        with pymupdf.open(stream=data, filetype="pdf") as document:
            return "\n\n".join(f"[Page {page.number + 1}]\n{page.get_text().strip() or '(no text)'}" for page in document)
    except Exception as exc:
        raise ValueError(f"{name} is not a readable PDF") from exc


class Source:
    """A filesystem-backed data source with capability flags and a traversal guard."""

    def __init__(
        self,
        name: str,
        root: Path,
        *,
        readable: bool = True,
        writable: bool = False,
        indexed: bool = False,
    ):
        self.name = name
        self.root = Path(root)
        self.readable = readable
        self.writable = writable
        self.indexed = indexed
        self.root.mkdir(parents=True, exist_ok=True)

    def _real_root(self) -> Path:
        """The root with its links followed, worked out once: every path check compares against it."""
        real = self.__dict__.get("_real_root_path")
        if real is None:
            real = self.__dict__["_real_root_path"] = self.root.resolve()
        return real

    def resolve(self, rel_path: str) -> Path:
        """Resolve a path relative to the source root, rejecting anything that escapes it."""
        target = (self.root / rel_path).resolve()
        root = self._real_root()
        if target != root and root not in target.parents:
            raise ValueError(f"path escapes source '{self.name}': {rel_path!r}")
        return target

    def exists(self, rel_path: str) -> bool:
        return self.resolve(rel_path).exists()

    def read(self, rel_path: str) -> str:
        if not self.readable:
            raise PermissionError(f"source '{self.name}' is not readable")
        return self.resolve(rel_path).read_text(encoding="utf-8")

    def read_bytes(self, rel_path: str) -> bytes:
        """The raw bytes of a file (for moves and binary files such as PDFs); `read` stays the text reader."""
        if not self.readable:
            raise PermissionError(f"source '{self.name}' is not readable")
        return self.resolve(rel_path).read_bytes()

    def write(self, rel_path: str, content: str) -> Path:
        if not self.writable:
            raise PermissionError(f"source '{self.name}' is read-only")
        path = self.resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace_text(path, content)
        return path

    def write_bytes(self, rel_path: str, data: bytes) -> Path:
        """Write a binary file the same way `write` writes text (same checks, parent folders created)."""
        if not self.writable:
            raise PermissionError(f"source '{self.name}' is read-only")
        path = self.resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace_bytes(path, data)
        return path

    def delete(self, rel_path: str) -> None:
        if not self.writable:
            raise PermissionError(f"source '{self.name}' is read-only")
        path = self.resolve(rel_path)
        if path.exists():
            path.unlink()

    def list(self, subpath: str = "") -> list[str]:
        """List the immediate entries under *subpath* (folders end with '/')."""
        if not self.readable:
            raise PermissionError(f"source '{self.name}' is not readable")
        base = self.resolve(subpath)
        if not base.is_dir():
            return []
        root = self.root.resolve()
        entries = []
        for child in base.iterdir():
            rel = child.relative_to(root).as_posix()
            entries.append(rel + "/" if child.is_dir() else rel)
        return sorted(entries)

    def index_scan(self, exts: set[str]) -> dict[str, tuple[int, int]]:
        """Files the vector index covers, as `{relative path: (mtime_ns, size)}`; hidden entries are skipped."""
        return scan_files(self.root.resolve(), exts)

    def walk(self, exts: set[str] | None = None) -> list[str]:
        """Recursively list file paths under the source, optionally filtered by extension."""
        root = self.root.resolve()
        out = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if exts and path.suffix.lstrip(".").lower() not in exts:
                continue
            out.append(path.relative_to(root).as_posix())
        return sorted(out)
