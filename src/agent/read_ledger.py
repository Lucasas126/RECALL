"""What each file looked like when this chat last read or wrote it, so a whole-file write cannot silently drop a change
another chat, task or dream made in between (optimistic concurrency: compare, then write under the file's lock)."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path


def file_digest(path: Path) -> str | None:
    """sha256 of a file's bytes, or None when there is no file."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def bytes_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReadLedger:
    """Per-chat map of resolved file path to the digest of the bytes this chat last saw there.

    Every check runs inside the file's write lock, so "compare, then write" cannot interleave with another writer.
    Content hashes, not modification times: a rewrite with the same bytes is not a change, and a change within the
    same mtime tick is still caught.
    """

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}
        self._guard = threading.Lock()

    def get(self, path: Path) -> str | None:
        with self._guard:
            return self._seen.get(str(path))

    def record(self, path: Path, digest: str | None) -> None:
        """Remember what the chat saw at `path`; None (no file) forgets it."""
        with self._guard:
            if digest is None:
                self._seen.pop(str(path), None)
            else:
                self._seen[str(path)] = digest

    def saw(self, path: Path) -> None:
        """Record the file as it is now (call it under the file's lock, right after this chat wrote it)."""
        self.record(path, file_digest(path))

    def forget(self, path: Path) -> None:
        self.record(path, None)

    def changed(self, path: Path, before: str | None) -> None:
        """After an edit or append: the chat knows the new file only if its view was current before the change
        (or the file is new); otherwise the next whole-file write must read the file again."""
        if before is None or self.get(path) == before:
            self.saw(path)
        else:
            self.forget(path)

    def overwrite_refusal(self, path: Path, address: str, read_with: str = "") -> str | None:
        """Why replacing the whole file at `path` must not happen now; None when it may (new file or current view).

        `read_with` names the tool that reads it, when it is not plain `read`."""
        how = f" with {read_with}" if read_with else ""
        current = file_digest(path)
        if current is None:
            return None
        known = self.get(path)
        if known is None:
            return (
                f"error: {address} already exists and you have not read it in this chat. Nothing was written. "
                f"Read it{how} first and write the full new version, or change only a part of it."
            )
        if known != current:
            return (
                f"error: {address} changed since you last read it (another chat, task or dream wrote to it). "
                f"Nothing was written. Read it again{how} and redo your change, or change only a part of it."
            )
        return None

    def delete_refusal(self, path: Path, address: str) -> str | None:
        """Why deleting `path` must not happen now: the chat read it and it changed since; never read is allowed."""
        known = self.get(path)
        if known is None:
            return None
        current = file_digest(path)
        if current is not None and current != known:
            return (
                f"error: {address} changed since you last read it (another chat, task or dream wrote to it). "
                "Nothing was deleted. Read it again before deciding to delete it."
            )
        return None
