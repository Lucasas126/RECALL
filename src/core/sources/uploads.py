from __future__ import annotations

from pathlib import Path

from src.core.sources.base import Source

UPLOADS = "uploads"


class UploadsSource(Source):
    """The user's saved copies of attached files (`<chat_id>/<file>`): readable, never writable, not indexed."""

    def __init__(self, root: Path):
        super().__init__(UPLOADS, root, readable=True, writable=False, indexed=False)

    def write(self, rel_path: str, content: str) -> Path:
        raise PermissionError(f"source '{self.name}' holds the user's uploaded files and is read-only")

    def write_bytes(self, rel_path: str, data: bytes) -> Path:
        raise PermissionError(f"source '{self.name}' holds the user's uploaded files and is read-only")

    def delete(self, rel_path: str) -> None:
        raise PermissionError(f"source '{self.name}' holds the user's uploaded files and is read-only")
