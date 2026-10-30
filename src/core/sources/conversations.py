from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path

from src.core.sources.base import Source

_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TITLE_CHARS = 60


class ConversationsSource(Source):
    """Chats with the user: one JSONL file per chat (one turn per line), named `<chat_id>.jsonl`."""

    def __init__(self, root: Path):
        # not writable to the agent's tools; chats are managed via the methods below
        super().__init__("conversations", root, readable=True, writable=False, indexed=True)

    def _path(self, chat_id: str) -> Path:
        if not _CHAT_ID_RE.match(chat_id):
            raise ValueError(f"invalid chat id: {chat_id!r}")
        return self.resolve(f"{chat_id}.jsonl")

    def exists_chat(self, chat_id: str) -> bool:
        return self._path(chat_id).is_file()

    def create(self) -> str:
        """Create an empty chat and return its id."""
        chat_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        self._path(chat_id).touch(exist_ok=False)
        return chat_id

    def delete_chat(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if not path.is_file():
            raise FileNotFoundError(f"chat not found: {chat_id}")
        path.unlink()

    def append(self, chat_id: str, role: str, content: str, **extra) -> dict:
        """Append a turn to an existing chat as one JSONL line."""
        path = self._path(chat_id)
        if not path.is_file():
            raise FileNotFoundError(f"chat not found: {chat_id}")
        turn = {
            "role": role,
            "content": content,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            **extra,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
        return turn

    def load(self, chat_id: str) -> list[dict]:
        """Read all turns of a chat."""
        path = self._path(chat_id)
        if not path.is_file():
            raise FileNotFoundError(f"chat not found: {chat_id}")
        turns = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                turns.append(json.loads(line))
        return turns

    def chats(self) -> list[str]:
        """List chat ids, oldest first."""
        return sorted(p.stem for p in self.root.iterdir() if p.suffix == ".jsonl")

    def prune_empty(self, keep: str | None = None) -> list[str]:
        """Delete chats with no turns (never used), except *keep*; returns the removed ids."""
        removed = []
        for path in self.root.iterdir():
            if path.suffix != ".jsonl" or path.stem == keep:
                continue
            if not path.read_text(encoding="utf-8").strip():
                path.unlink(missing_ok=True)
                removed.append(path.stem)
        return removed

    def latest(self) -> str | None:
        """The id of the most recently modified chat, if any."""
        files = [p for p in self.root.iterdir() if p.suffix == ".jsonl"]
        return max(files, key=lambda p: p.stat().st_mtime).stem if files else None

    def summary(self, chat_id: str) -> dict:
        """Chat metadata: title (first user message), turn count and last activity."""
        turns = self.load(chat_id)
        first_user = next((t["content"] for t in turns if t.get("role") == "user"), "")
        title = " ".join(first_user.split())
        if len(title) > _TITLE_CHARS:
            title = title[: _TITLE_CHARS - 1] + "…"
        updated = turns[-1].get("ts") if turns else None
        if not updated:
            mtime = self._path(chat_id).stat().st_mtime
            updated = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
        return {"id": chat_id, "title": title, "turns": len(turns), "updated": updated}
