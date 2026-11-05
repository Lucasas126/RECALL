from __future__ import annotations

from datetime import datetime

from src.core.sources.conversations import ConversationsSource
from src.core.sources.dream import DreamStateSource
from src.utils.i18n import InputError

TURN_CHARS = 4000  # longest turn text shown to a dream; longer turns are cut


def _turn_text(content: object) -> str:
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            else:
                parts.append("[attachment]")
        content = " ".join(parts)
    text = str(content or "")
    return text if len(text) <= TURN_CHARS else text[:TURN_CHARS] + " …[cut]"


def render_turn(number: int, turn: dict) -> dict:
    """A turn as a dream sees it: position, role, time, text and the tools the agent used."""
    rendered = {"n": number, "role": turn.get("role"), "ts": turn.get("ts"), "content": _turn_text(turn.get("content"))}
    actions = turn.get("actions") or []
    if actions:
        rendered["tools"] = [action.get("name") for action in actions if isinstance(action, dict)]
    if turn.get("interrupted"):
        rendered["interrupted"] = True
    return rendered


class ReviewQueue:
    """Which chat turns a dream still has to review, tracked as a per-chat watermark.

    A chat is pending while it has more turns than its `reviewed_until`; new chats start at 0 and a
    chat that continues after a review becomes pending again with only the new turns. Chats that no
    longer exist are dropped from the state.
    """

    def __init__(self, conversations: ConversationsSource, state: DreamStateSource):
        self._conversations = conversations
        self._state = state

    def watermarks(self) -> dict[str, int]:
        chats = self._state.state()["chats"]
        return {chat_id: int(entry.get("reviewed_until", 0)) for chat_id, entry in chats.items()}

    def pending(self) -> list[dict]:
        """Pending chats, oldest unreviewed turn first: id, title, turn counts and first new turn time."""
        existing = set(self._conversations.chats(include_internal=True))  # a `remember` task chat is hidden but reviewable
        disposable = self._conversations.disposable_chats()
        stale = set(self._state.state()["chats"]) - existing
        if stale:
            def drop(state: dict) -> None:
                for chat_id in stale:
                    state["chats"].pop(chat_id, None)

            self._state.update_state(drop)
        marks = self.watermarks()
        items = []
        for chat_id in existing - disposable:  # disposable chats are never learned from
            turns = self._conversations.load(chat_id)
            reviewed = min(marks.get(chat_id, 0), len(turns))
            if len(turns) <= reviewed:
                continue
            first_new = turns[reviewed].get("ts") or ""
            items.append({
                "chat_id": chat_id,
                "title": self._conversations.summary(chat_id)["title"],
                "turns": len(turns),
                "reviewed_until": reviewed,
                "new_turns": len(turns) - reviewed,
                "first_new_at": first_new,
            })
        return sorted(items, key=lambda item: (item["first_new_at"], item["chat_id"]))

    def read(self, chat_id: str, max_turns: int = 40, context_turns: int = 2) -> dict:
        """The next unreviewed turns of a chat (at most `max_turns`) plus a few earlier turns for context.

        `read_until` is the watermark to store once these turns are reviewed; turns that arrive later,
        or that did not fit, stay pending.
        """
        if not self._conversations.exists_chat(chat_id):
            raise FileNotFoundError(f"chat not found: {chat_id}")
        if self._conversations.is_disposable(chat_id):
            raise InputError("chat {chat_id} is disposable and is not reviewed", chat_id=chat_id)
        turns = self._conversations.load(chat_id)
        reviewed = min(self.watermarks().get(chat_id, 0), len(turns))
        end = min(len(turns), reviewed + max(1, max_turns))
        start = max(0, reviewed - context_turns)
        return {
            "chat_id": chat_id,
            "title": self._conversations.summary(chat_id)["title"],
            "total_turns": len(turns),
            "reviewed_until": reviewed,
            "read_until": end,
            "remaining_after": len(turns) - end,
            "context": [render_turn(index + 1, turns[index]) for index in range(start, reviewed)],
            "turns": [render_turn(index + 1, turns[index]) for index in range(reviewed, end)],
        }

    def mark_reviewed(self, chat_id: str, until: int, now: datetime | None = None) -> int:
        """Advance a chat's watermark to `until` (never backwards) and return the stored value."""
        stamp = (now or datetime.now()).astimezone().isoformat(timespec="seconds")

        def change(state: dict) -> None:
            entry = state["chats"].setdefault(chat_id, {"reviewed_until": 0})
            entry["reviewed_until"] = max(int(entry.get("reviewed_until", 0)), until)
            entry["reviewed_at"] = stamp

        return self._state.update_state(change)["chats"][chat_id]["reviewed_until"]
