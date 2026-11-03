from __future__ import annotations

from pathlib import Path


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

    def resolve(self, rel_path: str) -> Path:
        """Resolve a path relative to the source root, rejecting anything that escapes it."""
        target = (self.root / rel_path).resolve()
        root = self.root.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"path escapes source '{self.name}': {rel_path!r}")
        return target

    def exists(self, rel_path: str) -> bool:
        return self.resolve(rel_path).exists()

    def read(self, rel_path: str) -> str:
        if not self.readable:
            raise PermissionError(f"source '{self.name}' is not readable")
        return self.resolve(rel_path).read_text(encoding="utf-8")

    def write(self, rel_path: str, content: str) -> Path:
        if not self.writable:
            raise PermissionError(f"source '{self.name}' is read-only")
        path = self.resolve(rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
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
