from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Collection, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config.models import ReasoningEffort
from src.core.sources.base import Source
from src.utils.files import atomic_write_text
from src.utils.i18n import InputError, UserMessageError

_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TITLE_CHARS = 60


class ChatModel(BaseModel):
    """A persistent chat-specific OpenRouter model selection."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["openrouter"] = "openrouter"
    id: str = Field(strict=True, min_length=1, max_length=256)
    zdr: bool = Field(default=True, strict=True)


class ChatReasoningEffort(BaseModel):
    """A chat-owned reasoning effort that replaces the agent default for that chat."""

    model_config = ConfigDict(extra="forbid")

    effort: ReasoningEffort


class ChatMetadataError(UserMessageError, RuntimeError):
    """Chat model metadata could not be read or saved."""


class ConversationsSource(Source):
    """Chats with the user: one JSONL file per chat (one turn per line), named `<chat_id>.jsonl`."""

    def __init__(self, root: Path):
        # not writable to the agent's tools; chats are managed via the methods below
        super().__init__("conversations", root, readable=True, writable=False, indexed=True)

    def _path(self, chat_id: str) -> Path:
        if not _CHAT_ID_RE.match(chat_id):
            raise InputError("invalid {kind} id: {value!r}", kind="chat", value=chat_id)
        return self.resolve(f"{chat_id}.jsonl")

    def exists_chat(self, chat_id: str) -> bool:
        return self._path(chat_id).is_file()

    def _model_path(self, chat_id: str) -> Path:
        if not self.exists_chat(chat_id):
            raise FileNotFoundError(f"chat not found: {chat_id}")
        return self.resolve(f"{chat_id}.model.json")

    def model_selection(self, chat_id: str) -> ChatModel | None:
        """Read a chat's model override; older chats have no override."""
        path = self._model_path(chat_id)
        try:
            return ChatModel.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValidationError) as exc:
            raise ChatMetadataError("Could not read model selection for chat {chat_id}.", chat_id=chat_id) from exc

    def set_model(self, chat_id: str, model_id: str, *, zdr: bool = True) -> ChatModel:
        """Persist an override after the caller validates model availability."""
        path = self._model_path(chat_id)
        selection = ChatModel(id=model_id, zdr=zdr)
        try:
            atomic_write_text(path, selection.model_dump_json() + "\n")
        except OSError as exc:
            raise ChatMetadataError("Could not save model selection for chat {chat_id}.", chat_id=chat_id) from exc
        return selection

    def clear_model(self, chat_id: str) -> None:
        """Remove the LLM override so the chat inherits the current user default."""
        try:
            self._model_path(chat_id).unlink(missing_ok=True)
        except OSError as exc:
            raise ChatMetadataError("Could not save model selection for chat {chat_id}.", chat_id=chat_id) from exc

    def reasoning_effort_selection(self, chat_id: str) -> ChatReasoningEffort | None:
        """Read a chat's reasoning effort override; None means the agent default applies."""
        self._model_path(chat_id)
        path = self.resolve(f"{chat_id}.effort.json")
        try:
            return ChatReasoningEffort.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValidationError) as exc:
            raise ChatMetadataError("Could not read reasoning effort for chat {chat_id}.", chat_id=chat_id) from exc

    def set_reasoning_effort(self, chat_id: str, effort: ReasoningEffort | None) -> None:
        """Persist or clear a chat's reasoning effort after the caller validates the model supports it."""
        self._model_path(chat_id)
        path = self.resolve(f"{chat_id}.effort.json")
        try:
            if effort is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, ChatReasoningEffort(effort=effort).model_dump_json() + "\n")
        except OSError as exc:
            raise ChatMetadataError("Could not save reasoning effort for chat {chat_id}.", chat_id=chat_id) from exc

    def create(self) -> str:
        """Create an empty chat and return its id."""
        chat_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        self._path(chat_id).touch(exist_ok=False)
        return chat_id

    def delete_chat(self, chat_id: str) -> None:
        path = self._path(chat_id)
        if not path.is_file():
            raise FileNotFoundError(f"chat not found: {chat_id}")
        metadata = self._model_path(chat_id)
        effort = self.resolve(f"{chat_id}.effort.json")
        legacy_transcription = self.resolve(f"{chat_id}.stt.json")  # per-chat STT overrides are no longer read
        path.unlink()
        metadata.unlink(missing_ok=True)
        effort.unlink(missing_ok=True)
        legacy_transcription.unlink(missing_ok=True)

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

    def prune_empty(self, keep: str | Collection[str] | None = None) -> list[str]:
        """Delete unused chats except the given chat id(s); return the removed ids."""
        removed = []
        kept = {keep} if isinstance(keep, str) else set(keep or ())
        for path in self.root.iterdir():
            if path.suffix != ".jsonl" or path.stem in kept:
                continue
            if not path.read_text(encoding="utf-8").strip():
                self.delete_chat(path.stem)
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
        if isinstance(first_user, list):
            first_user = next(
                (part.get("text", "") for part in first_user if part.get("type") == "text"),
                "[attachment]",
            )
        title = " ".join(first_user.split())
        if len(title) > _TITLE_CHARS:
            title = title[: _TITLE_CHARS - 1] + "…"
        updated = turns[-1].get("ts") if turns else None
        if not updated:
            mtime = self._path(chat_id).stat().st_mtime
            updated = datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds")
        return {"id": chat_id, "title": title, "turns": len(turns), "updated": updated}
