from __future__ import annotations

import os
import threading
from contextlib import closing
from time import perf_counter
from typing import TYPE_CHECKING, Optional

from src.config.model_refs import is_claude_subscription
from src.config.settings import ASSOC
from src.core.chunking import excerpt
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.openrouter import OpenRouterProvider
from src.providers.provider import LLMProvider
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.agent import Agent

logger = get_logger("associative")

_SYSTEM_PROMPT = (
    "You are the associative-memory module of an assistant that has long-term memory "
    "(past notes and conversations).\n"
    "Given the recent conversation and the latest user message, decide whether searching that "
    "memory would help answer.\n"
    "If it would, reply with ONLY a short search query of key concepts/entities "
    "(3-6 words, no full sentences, no punctuation), written in the SAME LANGUAGE as the user "
    "so it matches how the memory was stored.\n"
    "Always search when the message touches the user's own life (their pets, people, plans, trips, "
    "preferences, projects or earlier conversations), even if the request is creative or general.\n"
    "If no lookup is needed (greetings, small talk, or nothing to recall), reply with exactly: NONE\n"
    "Output only the query or NONE, nothing else."
)

_FRAGMENT_LIMIT = 400
# longest message searched as is in `question_only` mode
_QUESTION_LIMIT = 1000


def recall(
    agent: "Agent", user_input: str, cancellation: CancellationToken | None = None,
    history: list[dict] | None = None, chat_id: str | None = None,
) -> tuple[Optional[str], int]:
    """Return a transient hint block and the number of matching memory fragments.

    Chunks of `chat_id` (the chat being answered, already in the context) are never suggested.
    """
    started_at = perf_counter()
    if agent.config.associative_memory == "question_only":
        query = user_input.strip()[:_QUESTION_LIMIT] or None
    else:
        query = _propose_query(agent, user_input, cancellation, history)
    proposed_at = perf_counter()
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    if not query:
        logger.info("no lookup needed (query proposal %.0f ms)", (proposed_at - started_at) * 1000)
        return None, 0
    found = agent.query_memory(query, ASSOC.top_n, exclude_chat=chat_id)
    logger.info(
        "query=%r proposed in %.0f ms, memory search took %.0f ms",
        query, (proposed_at - started_at) * 1000, (perf_counter() - proposed_at) * 1000,
    )
    hits = [h for h in found if h["score"] >= ASSOC.score_threshold][:ASSOC.top_n]
    if cancellation is not None:
        cancellation.raise_if_cancelled()
    if not hits:
        logger.debug("query=%r -> no hits above threshold %.2f", query, ASSOC.score_threshold)
        return None, 0
    logger.info("query=%r -> %d hit(s)", query, len(hits))
    lines = "\n".join(f"- [{_label(h)}] {_fragment(h.get('body', h['document']))}" for h in hits)
    suggestion = (
        "[associative memory] possibly-relevant fragments from your own memory "
        "(hints only — verify with your tools before relying on them):\n" + lines
    )
    return suggestion, len(hits)


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
        self._result: tuple[Optional[str], int] = (None, 0)
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
    if is_claude_subscription(ASSOC.query_model):
        return agent.claude_provider("associative_memory.query_model"), ASSOC.query_model, ASSOC.zdr
    api_key = os.environ.get(ASSOC.api_key, "").strip()
    if not api_key:
        raise RuntimeError(f"associative query model needs the env var {ASSOC.api_key} to be set")
    return OpenRouterProvider(api_key=api_key), ASSOC.query_model, ASSOC.zdr


def _propose_query(
    agent: "Agent", user_input: str, cancellation: CancellationToken | None = None,
    history: list[dict] | None = None,
) -> Optional[str]:
    """Ask the memory subagent (trimmed context) for a search query, or None when no lookup is needed."""
    recent = [
        {"role": m["role"], "content": m["content"]}
        for m in (agent.messages[1:] if history is None else history)
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-ASSOC.context_messages:] if ASSOC.context_messages else []
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, *recent, {"role": "user", "content": user_input}]
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
    text = text.strip()
    if not text or text.upper().startswith("NONE"):
        return None
    return text.splitlines()[0].strip().strip('"').strip()


def _fragment(text: str, limit: int = _FRAGMENT_LIMIT) -> str:
    """A single-line excerpt of at most `limit` characters, cut at a sentence end when possible."""
    return excerpt(text, limit)
