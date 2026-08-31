from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from src.core.sources.base import Source


class ConversationsSource(Source):
    """Past conversations with the user, one JSONL file per session (one turn per line)."""

    def __init__(self, root: Path):
        super().__init__("conversations", root, readable=True, writable=True, indexed=True)

    def start(self, system_prompt: str | None = None) -> str:
        """Begin a new conversation file and return its id (relative path)."""
        conversation = f"{datetime.now().astimezone():%Y%m%d_%H%M%S%z}.jsonl"
        if system_prompt:
            self.append(conversation, "system", system_prompt)
        else:
            self.resolve(conversation).touch()
        return conversation

    def append(self, conversation: str, role: str, content: str, **extra) -> dict:
        """Append a turn to a conversation as one JSONL line."""
        path = self.resolve(conversation)
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
        """Read all turns of a conversation."""
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
        """List conversation ids, most recent last."""
        return sorted(p.name for p in self.root.iterdir() if p.suffix == ".jsonl")

