"""Server-side copies of the files users attach to chats.

Files live in `<data_dir>/<user_id>/.uploads/<chat_id>/<timestamp>-<name>`, a sibling of the agents' data folders
(like the profile pictures): every agent of the same user can be given read access to it, no other user can. A dot-folder
can never clash with an agent id. The original bytes are stored exactly as received, before any conversion.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from src.utils.i18n import InputError, UserMessageError

UPLOADS_DIR = ".uploads"
MAX_NAME_CHARS = 80
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_USER = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_UNSAFE = re.compile(r"[^\w.\- ]+", re.UNICODE)


class UploadStorageError(UserMessageError, RuntimeError):
    """The copy could not be written (an actionable message for the owner)."""

    code = "upload_storage"


def sanitise_name(name: str) -> str:
    """A file name safe to store: no separators or control characters, no leading dots, bounded length (extension kept)."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = _UNSAFE.sub("_", base).strip(" .")
    base = re.sub(r"\s+", " ", base).lstrip(".")
    if not base:
        return "file"
    stem, dot, extension = base.rpartition(".")
    if not stem:  # no extension
        return base[:MAX_NAME_CHARS]
    extension = extension[:16]
    return f"{stem[:max(1, MAX_NAME_CHARS - len(extension) - 1)]}.{extension}"


class UploadStore:
    """Writes, finds and expires the copies under the data folder."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def folder(self, user_id: str) -> Path:
        """The user's uploads folder (not created)."""
        if not _USER.fullmatch(user_id or "") or user_id in (".", ".."):
            raise ValueError("invalid user id for uploads")
        return self.data_dir / user_id / UPLOADS_DIR

    def save(self, user_id: str, chat_id: str, name: str, data: bytes, *, now: datetime | None = None) -> Path:
        """Store `data` as a new file of the chat and return its path; an existing file is never overwritten."""
        if not _ID.fullmatch(chat_id or ""):
            raise ValueError("invalid chat id for uploads")
        root = self.folder(user_id)
        stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
        leaf = f"{stamp}-{sanitise_name(name)}"
        try:
            folder = root / chat_id
            for part in (root, folder):
                if part.is_symlink():
                    raise UploadStorageError("Could not save the uploaded file: the uploads folder is a link.")
            folder.mkdir(parents=True, exist_ok=True)
            if folder.resolve().parent != root.resolve():
                raise UploadStorageError("Could not save the uploaded file: the uploads folder is not valid.")
            stem, dot, extension = leaf.rpartition(".")
            if not stem:
                stem, dot, extension = leaf, "", ""
            for number in range(1000):
                candidate = folder / (leaf if number == 0 else f"{stem}-{number}{dot}{extension}")
                try:
                    with NamedTemporaryFile(dir=folder, prefix=".", suffix=".tmp", delete=False) as handle:
                        temporary = Path(handle.name)
                        handle.write(data)
                    try:
                        os.link(temporary, candidate)  # fails when the name exists: nothing is replaced
                    finally:
                        temporary.unlink(missing_ok=True)
                    return candidate
                except FileExistsError:
                    continue
        except OSError as exc:
            raise UploadStorageError("Could not save the uploaded file; check the data folder permissions.") from exc
        raise UploadStorageError("Could not find a free name for the uploaded file.")

    def expire(self, retention_days: int, *, now: float | None = None) -> int:
        """Remove every copy of every user older than `retention_days` (by modification time); returns how many."""
        cutoff = (time.time() if now is None else now) - retention_days * 86400
        removed = 0
        if not self.data_dir.is_dir():
            return 0
        for user_dir in self.data_dir.iterdir():
            root = user_dir / UPLOADS_DIR
            if root.is_symlink() or not root.is_dir():
                continue
            for chat_dir in root.iterdir():
                if chat_dir.is_symlink() or not chat_dir.is_dir():
                    continue
                for path in chat_dir.iterdir():
                    try:
                        if path.is_symlink() or not path.is_file() or path.stat().st_mtime >= cutoff:
                            continue
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue
                try:
                    chat_dir.rmdir()  # only when empty
                except OSError:
                    pass
        return removed
