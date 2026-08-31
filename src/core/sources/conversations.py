from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src.core.sources.base import Source


class ConversationsSource(Source):
    """Past conversations with the user, one JSONL file per UTC day (one turn per line)."""

    def __init__(self, root: Path):
        super().__init__("conversations", root, readable=True, writable=True, indexed=True)

    def current(self) -> str:
        """The current day's conversation file id (rolls over at UTC midnight)."""
        return f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"

    def append(self, role: str, content: str, **extra) -> dict:
        """Append a turn to today's conversation as one JSONL line."""
        path = self.resolve(self.current())
        path.parent.mkdir(parents=True, exist_ok=True)
        turn = {
            "role": role,
            "content": content,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            **extra,
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
        return turn

    def load(self, conversation: str) -> list[dict]:
        """Read all turns of a given day's conversation."""
        path = self.resolve(conversation)
        if not path.exists():
            return []
        turns = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                turns.append(json.loads(line))
        return turns

    def conversations(self) -> list[str]:
        """List conversation ids (days), most recent last."""
        return sorted(p.name for p in self.root.iterdir() if p.suffix == ".jsonl")

    def last_turns(self, n: int) -> tuple[list[dict], bool]:
        """Return the last *n* turns across day files, and whether older turns were dropped.

        Walks day files newest-first until it has n turns or runs out.
        """
        if n <= 0:
            return [], False
        files = self.conversations()  # oldest -> newest
        collected: list[dict] = []
        consumed = 0
        for day in reversed(files):
            collected = self.load(day) + collected
            consumed += 1
            if len(collected) >= n:
                break
        truncated = len(collected) > n or consumed < len(files)
        return collected[-n:], truncated

