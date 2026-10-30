from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from src.config.settings import ASSOC_CONTEXT_MESSAGES, ASSOC_SCORE_THRESHOLD, ASSOC_TOP_N
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
    "If no lookup is needed (greetings, small talk, or nothing to recall), reply with exactly: NONE\n"
    "Output only the query or NONE, nothing else."
)

_FRAGMENT_LIMIT = 200


def recall(agent: "Agent", user_input: str) -> Optional[str]:
    """Propose a query via the memory subagent, search, and return a transient hint block or None."""
    query = _propose_query(agent, user_input)
    if not query:
        return None
    hits = [h for h in agent.query_memory(query, ASSOC_TOP_N) if h["score"] >= ASSOC_SCORE_THRESHOLD]
    if not hits:
        logger.debug("query=%r -> no hits above threshold %.2f", query, ASSOC_SCORE_THRESHOLD)
        return None
    logger.info("query=%r -> %d hit(s) injected", query, len(hits))
    lines = "\n".join(f"- [{h['address']}] {_fragment(h['document'])}" for h in hits)
    return (
        "[associative memory] possibly-relevant fragments from your own memory "
        "(hints only — verify with your tools before relying on them):\n" + lines
    )


def _propose_query(agent: "Agent", user_input: str) -> Optional[str]:
    """Ask the memory subagent (trimmed context) for a search query, or None."""
    recent = [
        {"role": m["role"], "content": m["content"]}
        for m in agent.messages[1:]
        if m.get("role") in ("user", "assistant") and m.get("content")
    ][-ASSOC_CONTEXT_MESSAGES:]
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}, *recent, {"role": "user", "content": user_input}]
    try:
        # streamed to stay reliable on models whose non-streaming content can come back empty (e.g. R1)
        text = ""
        for chunk in agent.provider.invoke(
            messages, agent.config.models.chat, stream=True, reasoning=False, tools=None
        ):
            if isinstance(chunk, tuple) and chunk[0] == "content":
                text += chunk[1]
            elif isinstance(chunk, dict):
                text = chunk.get("response") or text
    except Exception as e:  # a failed lookup must never break the turn
        logger.warning("query proposal failed: %s", e)
        return None
    text = text.strip()
    if not text or text.upper().startswith("NONE"):
        return None
    return text.splitlines()[0].strip().strip('"').strip()


def _fragment(text: str, limit: int = _FRAGMENT_LIMIT) -> str:
    """A single-line, length-capped excerpt (never a whole note)."""
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"
