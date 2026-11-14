from __future__ import annotations

from pathlib import Path

from src import knowledge
from src.core.sources.base import Source, scan_files

RECALL_DOCS = "recall_docs"


class RecallDocsSource(Source):
    """The built-in `recall` manual (src/knowledge/recall/): indexed and readable, never writable."""

    def __init__(self, root: Path | None = None):
        super().__init__(RECALL_DOCS, root or knowledge.KNOWLEDGE_DIR, readable=True, writable=False, indexed=True)

    def write(self, rel_path: str, content: str) -> Path:
        raise PermissionError(f"source '{self.name}' holds the built-in docs and is read-only")

    def write_bytes(self, rel_path: str, data: bytes) -> Path:
        raise PermissionError(f"source '{self.name}' holds the built-in docs and is read-only")

    def delete(self, rel_path: str) -> None:
        raise PermissionError(f"source '{self.name}' holds the built-in docs and is read-only")

    def index_scan(self, exts: set[str]) -> dict[str, tuple[int, int]]:
        """Only the manual's Markdown pages; hidden entries and `__pycache__` are skipped."""
        found = scan_files(self.root.resolve(), {"md"} & exts if exts else {"md"})
        return {rel: stat for rel, stat in found.items() if "__pycache__" not in rel.split("/")}
