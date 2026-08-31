from __future__ import annotations

import json
from typing import Iterator, Optional, Union

import src.providers.openrouter  # noqa: F401  (registers the openrouter provider)
import src.providers.ollama  # noqa: F401  (registers the ollama provider)
from src.agent import usage
from src.agent.tools import build_tools
from src.config.loader import load_agent
from src.config.models import ResolvedAgent
from src.config.settings import AGENT_MAX_TOOL_HOPS
from src.core.index import VectorIndex
from src.core.sources.registry import SourceRegistry
from src.utils.logger import get_logger
from src.providers.provider import LLMProvider, get_provider

logger = get_logger("agent")


class Agent:
    """Orchestration entry point: wraps an agent's config, provider and conversation memory."""

    def __init__(self, config: ResolvedAgent, provider: LLMProvider):
        self.config = config
        self.provider = provider
        # per-agent access to the data plane (mind palace, conversations, ...)
        self.sources = SourceRegistry.for_agent(config.user_id, config.id)
        # semantic index over the indexed sources; kept in sync as memory changes
        self.index = VectorIndex.for_agent(
            config.user_id, config.id, self.sources,
            self._embeddings_provider(), config.models.embeddings,
        )
        self.index.sync()
        # tools the model may call (memory recall, source read/write/list)
        self._tool_specs, self._tool_handlers = build_tools(self) if config.tools else ([], {})
        self._max_context = None  # model context window, fetched lazily from the provider
        # real input tokens of the current context (seeded on load, updated each turn)
        self._ctx_input_tokens: Optional[int] = None
        # previous usage breakdown, to compute per-category turn deltas
        self._prev_breakdown: Optional[dict] = None
        # conversation history is the memory
        self.messages: list[dict] = [
            {"role": "system", "content": config.personality_prompt}
        ]
        # whether older turns were dropped from the in-context window
        self._truncated = False
        self._load_recent_context()
        # thinking tokens from the last reply, kept apart from the answer
        self.last_reasoning: str = ""
        # formatted usage breakdown of the last reply (for the caller to display)
        self.last_usage: str = ""
        self._reconstruct_context()
        logger.debug("initialized agent: %s", config)

    @classmethod
    def from_config(cls, user_id: str, agent_id: Optional[str] = None) -> "Agent":
        config = load_agent(user_id, agent_id)
        provider = get_provider(config.provider_name, api_key=config.api_key)
        logger.debug("loaded agent config: %s", config)
        return cls(config, provider)

    @property
    def name(self) -> str:
        return self.config.name

    def _embeddings_provider(self) -> LLMProvider:
        """The provider used for embeddings, reusing the chat provider when identical."""
        if (
            self.config.embeddings_provider_name == self.config.provider_name
            and self.config.embeddings_api_key == self.config.api_key
        ):
            return self.provider
        return get_provider(
            self.config.embeddings_provider_name, api_key=self.config.embeddings_api_key
        )

    def query_memory(self, query: str, top_k: int = 5) -> list[dict]:
        """Semantic recall over the agent's indexed memory (exposed to the model as a tool)."""
        self.index.sync()
        results = self.index.query(query, top_k)
        logger.debug("query_memory %r -> %d hits", query, len(results))
        return results

    def invoke(self, user_input: str, stream: Optional[bool] = None) -> Union[str, Iterator[str]]:
        """Send a user message and return the reply, updating conversation memory.

        Returns a string when not streaming, or a generator of text chunks when streaming.
        """
        if stream is None:
            stream = self.config.stream
        logger.info("invoke user=%s agent=%s stream=%s", self.config.user_id, self.config.id, stream)
        # without memory, each turn starts fresh from the system prompt
        if not self.config.has_memory:
            self.messages = [{"role": "system", "content": self.config.personality_prompt}]
            self._truncated = False
        self.messages.append({"role": "user", "content": user_input})
        self._log_turn("user", user_input)
        self._trim_context()
        return self._stream() if stream else self._complete()

    def _stream(self) -> Iterator[tuple]:
        for hop in range(AGENT_MAX_TOOL_HOPS + 1):
            tools = None if hop == AGENT_MAX_TOOL_HOPS else (self._tool_specs or None)
            context = self._context()
            reasoning, reply, api_usage, tool_calls = "", "", None, None
            for chunk in self.provider.invoke(
                context, self.config.models.chat,
                stream=True, reasoning=self.config.reasoning,
                reasoning_tokens=self.config.reasoning_tokens, tools=tools,
            ):
                if isinstance(chunk, tuple):
                    kind, text = chunk
                    if kind == "reasoning":
                        reasoning += text
                    else:
                        reply += text
                    yield chunk
                elif isinstance(chunk, dict):
                    api_usage = chunk.get("usage")
                    tool_calls = chunk.get("tool_calls")
            if tool_calls:
                self._remember_tool_calls(reply, reasoning, tool_calls)
                self._run_tools(tool_calls)
                continue
            self.last_reasoning = reasoning
            self._remember_reply(reply, reasoning, api_usage)
            self._track_usage(context, reply, reasoning, api_usage)
            return

    def _complete(self) -> str:
        for hop in range(AGENT_MAX_TOOL_HOPS + 1):
            tools = None if hop == AGENT_MAX_TOOL_HOPS else (self._tool_specs or None)
            context = self._context()
            result = self.provider.invoke(
                context, self.config.models.chat,
                stream=False, reasoning=self.config.reasoning,
                reasoning_tokens=self.config.reasoning_tokens, tools=tools,
            )
            reasoning = result.get("reasoning") or ""
            reply = result.get("response") or ""
            tool_calls = result.get("tool_calls")
            if tool_calls:
                self._remember_tool_calls(reply, reasoning, tool_calls)
                self._run_tools(tool_calls)
                continue
            self.last_reasoning = reasoning
            self._remember_reply(reply, reasoning, result.get("usage"))
            self._track_usage(context, reply, reasoning, result.get("usage"))
            return reply
        return ""  # unreachable: the final hop runs without tools and returns

    def _remember_tool_calls(self, reply: str, reasoning: str, tool_calls: list[dict]) -> None:
        """Append the model's tool-call turn to the in-context memory (OpenAI shape)."""
        self.messages.append(
            {
                "role": "assistant",
                "content": reply or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"]
                            if isinstance(tc["arguments"], str)
                            else json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )

    def _run_tools(self, tool_calls: list[dict]) -> None:
        """Execute each requested tool and append its result to the context."""
        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
            handler = self._tool_handlers.get(name)
            if handler is None:
                result = f"error: unknown tool '{name}'"
            else:
                try:
                    result = handler(**args)
                except Exception as e:  # surfaced back to the model, not fatal
                    result = f"error: {e}"
            logger.info("tool %s(%s)", name, args)
            self.messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "name": name, "content": result}
            )

    def _remember_reply(self, reply: str, reasoning: str, api_usage) -> None:
        """Append the assistant reply to memory and persist the turn (with its real usage)."""
        message = {"role": "assistant", "content": reply}
        if self.config.keep_reasoning_in_memory and reasoning:
            message["reasoning"] = reasoning
        self.messages.append(message)
        extra = {"reasoning": reasoning or None}
        if api_usage:
            extra["usage"] = {"in": api_usage.get("prompt_tokens"), "out": api_usage.get("completion_tokens")}
        self._log_turn("assistant", reply, **extra)

    def _log_turn(self, role: str, content: str, **extra) -> None:
        """Persist a turn to the conversation jsonl (independent of in-context memory)."""
        extra = {k: v for k, v in extra.items() if v is not None}
        self.sources.conversations().append(role, content, **extra)

    # ------------------------------------------------------------------ context window

    def _load_recent_context(self) -> None:
        """Seed the context with the last N turns from past conversations (across days)."""
        n = self.config.conversation_messages_retained
        if not (self.config.has_memory and n):
            return
        history, truncated = self.sources.conversations().last_turns(n)
        for t in history:
            if t.get("role") not in ("user", "assistant"):
                continue
            self.messages.append({"role": t["role"], "content": t["content"]})
        # seed the context input size from the most recent persisted usage
        for t in reversed(history):
            u = t.get("usage")
            if u and u.get("in") is not None:
                self._ctx_input_tokens = u["in"]
                break
        self._truncated = truncated

    def _trim_context(self) -> None:
        """Keep only the last N turns (system prompt aside), flagging that older ones were cut."""
        n = self.config.conversation_messages_retained
        if not n:
            return
        rest = self.messages[1:]
        if len(rest) > n:
            kept = rest[-n:]
            # never start on an orphan tool result (its assistant tool_calls would be gone)
            while kept and kept[0].get("role") == "tool":
                kept.pop(0)
            self.messages = [self.messages[0]] + kept
            self._truncated = True

    def _context(self) -> list[dict]:
        """The messages actually sent to the model, with a cutoff marker if history was dropped."""
        if self._truncated:
            return [self.messages[0], {"role": "system", "content": "[CUTOFF CONVERSATION]"}] + self.messages[1:]
        return self.messages

    def _ensure_max_context(self) -> None:
        if self._max_context is None:
            self._max_context = self.provider.context_window(self.config.models.chat) or 0

    def _reconstruct_context(self) -> None:
        """On load, show how loaded the context is, using the last persisted real input tokens."""
        if self._ctx_input_tokens is None:
            return  # nothing loaded / no real data yet; wait for the first turn
        breakdown, approx = usage.context_breakdown(self.messages, self._ctx_input_tokens)
        self._ensure_max_context()
        self._prev_breakdown = breakdown
        self.last_usage = usage.format_context(breakdown, approx, self._max_context or None)
        logger.info("usage\n%s", self.last_usage)

    def _track_usage(self, context: list[dict], reply: str, reasoning: str, api_usage) -> None:
        """Split this turn's real in/out tokens and record how loaded the context now is."""
        api_usage = api_usage or {}
        prompt_tokens = api_usage.get("prompt_tokens")
        completion_tokens = api_usage.get("completion_tokens")
        breakdown, approx = usage.turn_breakdown(context, reply, reasoning, prompt_tokens, completion_tokens)
        if approx:
            logger.warning("token usage approximated with chars/4 (provider returned no usage)")
        self._ensure_max_context()
        self.last_usage = usage.format_turn(breakdown, self._prev_breakdown, approx, self._max_context or None)
        self._prev_breakdown = breakdown
        self._ctx_input_tokens = breakdown["in"]  # for the next reconstruct
        logger.info("usage\n%s", self.last_usage)
