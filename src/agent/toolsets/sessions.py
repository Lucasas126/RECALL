from __future__ import annotations

import json
from typing import TYPE_CHECKING, Annotated, Callable

from src.agent.tooling import tool

if TYPE_CHECKING:
    from src.agent.agent import Agent

_DEFAULT_LIMIT = 5
_MAX_LIMIT = 20
_BODY_CHARS = 600
_EXCERPT_RADIUS = 160
_CURRENT = "current"


def _excerpt(text: str, start: int, length: int) -> str:
    """A one-line window of `text` around a match."""
    begin = max(0, start - _EXCERPT_RADIUS)
    end = min(len(text), start + length + _EXCERPT_RADIUS)
    piece = " ".join(text[begin:end].split())
    return ("…" if begin else "") + piece + ("…" if end < len(text) else "")


def _phrase(query: str) -> str | None:
    """The text between double quotes when the whole query is quoted, else None."""
    query = query.strip()
    if len(query) > 2 and query[0] == query[-1] == '"':
        return query[1:-1].strip() or None
    return None


def build_session_tools(agent: Agent) -> list[Callable[..., str]]:
    """Build the tool that searches inside past conversations."""
    sources = agent.sources

    def conversations():
        source = sources.conversations()
        return source if source.readable else None

    @tool
    def session_search(
        query: Annotated[str, 'what to find; wrap it in double quotes ("exact words") for an exact text match'],
        limit: Annotated[int, "results to return (default 5, max 20)"] = _DEFAULT_LIMIT,
        chat: Annotated[str, "only this chat id; 'current' means this conversation; omit for all chats"] = "",
        include_current: Annotated[bool, "also search this conversation (default true)"] = True,
    ) -> str:
        """Search inside your past conversations with the user, this one included.
        Without quotes it matches by meaning over the indexed exchanges (the newest exchange of a chat is indexed
        after the reply, so it may be missing). With the whole query in double quotes it finds that exact text,
        case-insensitively, in every message of every chat, newest first, with no indexing delay.
        Each result gives the chat address, date and who spoke; read the chat file for more context."""
        source = conversations()
        if source is None:
            return "error: conversations are not readable here."
        limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
        current = getattr(agent, "chat_id", None)
        chat = chat.strip()
        if chat == _CURRENT:
            if not current:
                return "error: there is no current conversation."
            chat = current
        chat = chat.removesuffix(".jsonl").removeprefix("conversations:")
        if not include_current and chat and chat == current:
            return "no matches."
        phrase = _phrase(query)
        if phrase:
            return _literal(source, phrase, limit, chat, None if include_current else current)
        hits = agent.query_memory(
            query, limit, exclude_chat=None if include_current else current,
            only_source="conversations", only_chat=chat or None,
        )
        if not hits:
            return "no matching conversations."
        return "\n\n".join(
            f"[{hit['address']} > {hit.get('context', '')}] (score {hit['score']:.2f})\n"
            f"{hit.get('body', hit['document'])[:_BODY_CHARS]}"
            for hit in hits
        )

    return [session_search]


def _literal(source, phrase: str, limit: int, chat: str, skip_chat: str | None) -> str:
    """Exact, case-insensitive matches in the user and assistant messages of the visible chats, newest first."""
    needle = phrase.lower()
    private = source.disposable_chats() | source.task_chats()
    names = sorted(
        (rel for rel in source.walk({"jsonl"}) if "/" not in rel and rel.endswith(".jsonl")), reverse=True,
    )
    found: list[str] = []
    total = 0
    for rel in names:
        chat_id = rel[: -len(".jsonl")]
        if chat_id in private or chat_id == skip_chat or (chat and chat_id != chat):
            continue
        try:
            text = source.read(rel)
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if needle not in line.lower():  # cheap reject before parsing the line
                continue
            try:
                turn = json.loads(line)
            except ValueError:
                continue
            content = turn.get("content") if isinstance(turn, dict) else None
            if turn.get("role") not in ("user", "assistant") or not isinstance(content, str):
                continue
            at = content.lower().find(needle)
            if at < 0:
                continue
            total += 1
            if len(found) < limit:
                when = str(turn.get("ts", ""))[:16].replace("T", " ")
                found.append(f"[conversations:{rel}:{number}] {when} {turn['role']}: {_excerpt(content, at, len(needle))}")
    if not found:
        return "no matches."
    footer = f"showing {len(found)} of {total} matches" if total > len(found) else f"{total} match(es)"
    return "\n".join(found) + f"\n... ({footer}; raise limit or pass chat=<id> to narrow)"
