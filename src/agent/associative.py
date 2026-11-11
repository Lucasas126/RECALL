from __future__ import annotations

import os
import re
import threading
from contextlib import closing
from time import perf_counter
from typing import TYPE_CHECKING, Optional

from src.config.model_refs import is_claude_subscription
from src.config.secrets import get_secret
from src.config.settings import ASSOC
from src.config.zdr import ensure_zdr_compatible, zdr_required
from src.core.chunking import excerpt
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.openrouter import OpenRouterProvider
from src.providers.provider import LLMProvider
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.agent import Agent

logger = get_logger("associative")

_SYSTEM_PROMPT = (
    "You are the associative-memory module of an assistant. The assistant has a long-term memory of past notes and "
    "OTHER, earlier conversations with this user; the current conversation is never in it.\n"
    "You receive the recent conversation as a transcript and the user's latest message. You do NOT answer the user "
    "and you do NOT continue the conversation: you only decide whether to search that memory, and with what query.\n\n"
    "SEARCH when the latest message asks about, or depends on, something the memory may hold and the transcript "
    "does not: the user's own life (people, pets, home, plans, trips, preferences, possessions such as their PC), "
    "their projects and setups, decisions or work from earlier conversations, research done for them, a reference to "
    "the past (\"remember\", \"last time\", \"what we did\"), or a question about what the assistant knows of a "
    "topic. A greeting before such a question does not change this.\n"
    "Reply NONE when the message is only a greeting, thanks, small talk or a short confirmation/instruction to go on "
    "(\"yes\", \"ok\", \"do it\", \"launch them\"), when the transcript already holds what is needed (mid-task "
    "follow-ups), or for general knowledge with no link to this user.\n\n"
    "Query: 2-6 keywords naming the specific concepts or entities, no sentences, no punctuation, no markdown, "
    "no commands, in the user's language. Output exactly one line: the query, or NONE.\n\n"
    "Examples:\n"
    "\"hi! what do you know about my PC?\" -> my PC hardware specs\n"
    "\"hey, what do you know about Atlas?\" (any name: a project, tool, person or place) -> Atlas\n"
    "\"what did we decide about the backup?\" -> backup decision\n"
    "\"tell me more about the cathedral style\" (the user had research on it) -> cathedral style details\n"
    "\"ok, go ahead with step 3\" -> NONE\n"
    "\"thanks!\" -> NONE\n"
    "\"how long do mushrooms keep in the fridge?\" -> NONE"
)

# what the query model sees of the recent conversation: a transcript in one user message (not real turns, so the
# small model does not slip into answering as the assistant), each message cut to this many characters
_TRANSCRIPT_MESSAGE_CHARS = 600
# a proposal longer than this many words, or one that looks like prose/markdown/a command, is not a query
_QUERY_MAX_WORDS = 8
# NONE, also when the model translated it to the user's language
_NONE = re.compile(r"^\W*(none|ninguna|ninguno|nada|n/a)\b", re.IGNORECASE)
_NOT_A_QUERY = re.compile(r"^\s*(#|```|[-*>] |\d+\. |\$ )|[`*|{}<>;=]|--|\s/\S")

_FRAGMENT_LIMIT = 400
# excerpt length of the fragments shown to the user and kept in the saved turn
_EXCERPT_LIMIT = _FRAGMENT_LIMIT  # the user sees as much of each fragment as the model got
# longest message searched as is in `question_only` mode
_QUESTION_LIMIT = 1000


class RecallResult(tuple):
    """`(suggestion, count)` plus the structured `fragments` and the proposed `query` (None when none was proposed).

    Each fragment is `{"address", "label", "score", "excerpt"}`: never a full chunk, so it can be persisted.
    """

    fragments: list[dict]
    query: Optional[str]

    def __new__(
        cls, suggestion: Optional[str], count: int,
        fragments: list[dict] | None = None, query: Optional[str] = None,
    ) -> "RecallResult":
        result = super().__new__(cls, (suggestion, count))
        result.fragments = list(fragments or [])
        result.query = query
        return result


def max_matches(agent: "Agent") -> int:
    """The user's own cap when set, else the application default (`top_n`). The threshold is never per user."""
    return getattr(agent, "associative_max_matches", None) or ASSOC.top_n


def recall(
    agent: "Agent", user_input: str, cancellation: CancellationToken | None = None,
    history: list[dict] | None = None, chat_id: str | None = None,
) -> RecallResult:
    """Return a transient hint block and the number of matching memory fragments.

    The result also carries the structured hits (`fragments`) and, in `llm` mode, the proposed `query`.
    Chunks of `chat_id` (the chat being answered, already in the context) are never suggested.
    """
    started_at = perf_counter()
    if agent.config.associative_memory == "question_only":
        query = user_input.strip()[:_QUESTION_LIMIT] or None
        proposed = None  # the user's own message: not worth repeating
    else:
        query = proposed = _propose_query(agent, user_input, cancellation, history)
    proposed_at = perf_counter()
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    if not query:
        logger.info("no lookup needed (query proposal %.0f ms)", (proposed_at - started_at) * 1000)
        return RecallResult(None, 0)
    limit = max_matches(agent)
    found = agent.query_memory(query, limit, exclude_chat=chat_id)
    logger.info(
        "query=%r proposed in %.0f ms, memory search took %.0f ms",
        query, (proposed_at - started_at) * 1000, (perf_counter() - proposed_at) * 1000,
    )
    hits = [h for h in found if h["score"] >= ASSOC.score_threshold][:limit]
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    if not hits:
        logger.debug("query=%r -> no hits above threshold %.2f", query, ASSOC.score_threshold)
        return RecallResult(None, 0, query=proposed)
    logger.info("query=%r -> %d hit(s)", query, len(hits))
    lines = "\n".join(f"- [{_label(h)}] {_fragment(h.get('body', h['document']))}" for h in hits)
    suggestion = (
        "[associative memory] possibly-relevant fragments from your own memory "
        "(hints only — verify with your tools before relying on them):\n" + lines
    )
    return RecallResult(suggestion, len(hits), [_hit_view(h) for h in hits], proposed)


def _hit_view(hit: dict) -> dict:
    """What the user sees of a hit: its address, heading path or chat title, score and a short excerpt."""
    score = hit.get("score")
    return {
        "address": hit["address"],
        "label": hit.get("context") or None,
        "score": round(float(score), 4) if score is not None else None,
        "excerpt": _fragment(hit.get("body", hit["document"]), _EXCERPT_LIMIT),
    }


def _label(hit: dict) -> str:
    """A hit's address plus its heading path or chat title, e.g. `mind_palace:pets.md > Pets > Cats`."""
    context = hit.get("context")
    return f"{hit['address']} > {context}" if context else hit["address"]


class RecallJob:
    """Associative recall running in a worker thread while the main model already generates."""

    def __init__(self, agent: "Agent", history: list[dict], user_input: str, chat_id: str | None = None) -> None:
        self._agent = agent
        self._chat_id = chat_id
        self._history = history
        self._user_input = user_input
        self._token = CancellationToken()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._waker: CancellationToken | None = None
        self._cut_ok: threading.Event | None = None
        self._result: tuple[Optional[str], int] = RecallResult(None, 0)
        self.failed = False  # the lookup raised, as opposed to finding nothing
        self.started_at = perf_counter()
        self.finished_at: float | None = None
        self._thread = threading.Thread(target=self._run, name="associative-recall", daemon=True)

    def start(self) -> "RecallJob":
        self._thread.start()
        return self

    def ready(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the job finishes (or `timeout` seconds pass); whether it has finished."""
        return self._done.wait(timeout)

    def result(self) -> tuple[Optional[str], int]:
        """The suggestion and match count of a finished job; (None, 0) when nothing was found (see `failed`)."""
        return self._result

    def cancel(self) -> None:
        self._token.cancel()

    def has_hit(self) -> bool:
        return self._done.is_set() and bool(self._result[0])

    def attach(self, waker: CancellationToken, cut_ok: threading.Event) -> None:
        """Cancel `waker` once the job has a hit and `cut_ok` is set, i.e. the reply may be cut right now."""
        with self._lock:
            self._waker, self._cut_ok = waker, cut_ok
            if self.has_hit() and cut_ok.is_set():
                waker.cancel()

    def detach(self) -> None:
        with self._lock:
            self._waker = self._cut_ok = None

    def _run(self) -> None:
        try:
            self._result = recall(
                self._agent, self._user_input, self._token, history=self._history, chat_id=self._chat_id,
            )
        except GenerationCancelled:
            logger.debug("associative recall cancelled")
        except Exception:  # a failed lookup must never break the turn
            self.failed = True
            logger.exception("associative recall failed")
        finally:
            with self._lock:
                self.finished_at = perf_counter()
                self._done.set()
                if self._result[0] and self._waker is not None and self._cut_ok.is_set():
                    self._waker.cancel()


def _query_model(agent: "Agent") -> tuple[LLMProvider, str, bool]:
    """The provider, model and ZDR flag for query proposal: the configured small model, else the chat's own."""
    if ASSOC.query_model is None:
        return agent.provider, agent.config.models.chat, agent.zdr_required
    zdr = zdr_required(agent.config.require_zdr, ASSOC.zdr)
    if is_claude_subscription(ASSOC.query_model):
        ensure_zdr_compatible(ASSOC.query_model, zdr, "associative_memory")
        return agent.claude_provider("associative_memory.query_model"), ASSOC.query_model, zdr
    api_key = get_secret(ASSOC.api_key).strip()
    if not api_key:
        raise RuntimeError(f"associative query model needs the env var {ASSOC.api_key} to be set")
    return OpenRouterProvider(api_key=api_key), ASSOC.query_model, zdr


def _propose_query(
    agent: "Agent", user_input: str, cancellation: CancellationToken | None = None,
    history: list[dict] | None = None,
) -> Optional[str]:
    """Ask the memory subagent (trimmed context) for a search query, or None when no lookup is needed."""
    recent = [
        m for m in (agent.messages[1:] if history is None else history)
        if m.get("role") in ("user", "assistant") and _text(m.get("content"))
    ][-ASSOC.context_messages:] if ASSOC.context_messages else []
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": _transcript(recent, user_input)}]
    provider, model, zdr = _query_model(agent)
    # streamed to stay reliable on models whose non-streaming content can come back empty (e.g. R1)
    text = ""
    with closing(provider.invoke(
        messages, model, stream=True, reasoning=False, tools=None,
        cancellation=cancellation,
        zdr=zdr,
    )) as chunks:
        for chunk in chunks:
            if isinstance(chunk, tuple) and chunk[0] == "content":
                text += chunk[1]
            elif isinstance(chunk, dict):
                text = chunk.get("response") or text
    return _clean_query(text)


def _clean_query(text: str) -> Optional[str]:
    """The proposed query, or None for NONE and for output that is not a keyword query (an answer, markdown, a command)."""
    text = text.strip()
    if not text or _NONE.match(text):
        return None
    line = text.splitlines()[0].strip().strip('"').strip()
    if not line or len(line.split()) > _QUERY_MAX_WORDS or _NOT_A_QUERY.search(line):
        logger.info("query proposal discarded (not a keyword query): %r", line[:120])
        return None
    return line


def _text(content: object) -> str:
    """The text of a message: a string, or the text parts of a multimodal list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return ""


def _transcript(recent: list[dict], user_input: str) -> str:
    """The recent conversation and the latest message as one block of quoted data for the query model."""
    def cut(text: str) -> str:
        text = " ".join(text.split())
        return text if len(text) <= _TRANSCRIPT_MESSAGE_CHARS else text[:_TRANSCRIPT_MESSAGE_CHARS] + " [...]"
    lines = [f"{m['role'].upper()}: {cut(_text(m.get('content')))}" for m in recent]
    return (
        "<transcript>\n" + ("\n".join(lines) if lines else "(no earlier messages)") + "\n</transcript>\n"
        f"<latest_user_message>\n{cut(user_input)}\n</latest_user_message>\n"
        "Search query or NONE:"
    )


def _fragment(text: str, limit: int = _FRAGMENT_LIMIT) -> str:
    """A single-line excerpt of at most `limit` characters, cut at a sentence end when possible."""
    return excerpt(text, limit)
