from __future__ import annotations

import json
from datetime import datetime
from time import perf_counter
from contextlib import closing
from typing import Iterator, Optional, Union

import src.providers.openrouter  # noqa: F401  (registers the openrouter provider)
import src.providers.ollama  # noqa: F401  (registers the ollama provider)
from src.agent import usage
from src.agent import associative
from src.agent.tools import build_tools
from src.config.loader import load_agent
from src.config.models import ModelsConfig, ResolvedAgent
from src.config.settings import AGENT_MAX_TOOL_HOPS, TAVILY
from src.core.index import VectorIndex
from src.core.sources.registry import SourceRegistry
from src.utils.logger import get_logger
from src.providers.provider import (
    LLMProvider,
    UnsupportedModalityError,
    get_provider,
    unsupported_modality_message,
)
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.tavily import TavilyClient

logger = get_logger("agent")

# appended to the context on the final tool hop to force a direct answer
_FINAL_NUDGE = "Stop calling tools. Answer the user now using what you already have."
# max characters of a tool result shown to channels and stored with the turn
_ACTION_RESULT_CHARS = 500
_INTERRUPTED_MARKER = "[Response interrupted by user.]"


def _dedupe_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Drop exact-duplicate (name, arguments) calls a model may emit within one turn."""
    seen: set = set()
    out: list[dict] = []
    for tc in tool_calls:
        args = tc["arguments"]
        key = (tc["name"], args if isinstance(args, str) else json.dumps(args, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        out.append(tc)
    return out


class Agent:
    """Orchestration entry point: wraps an agent's config, provider and conversation memory."""

    def __init__(self, config: ResolvedAgent, provider: LLMProvider, chat_id: Optional[str] = None):
        self.config = config.model_copy(deep=True)
        self._default_chat_model = config.models.chat
        self._default_reasoning_effort = config.reasoning_effort
        self.provider = provider
        # per-agent access to the data plane (mind palace, conversations, ...)
        self.sources = SourceRegistry.for_agent(config.user_id, config.id)
        # semantic index over the indexed sources; kept in sync as memory changes
        self.index = VectorIndex.for_agent(
            config.user_id, config.id, self.sources,
            self._embeddings_provider(), config.models.embeddings,
        )
        self.index.sync()
        self._tool_specs, self._tool_handlers = (
            build_tools(self, TavilyClient.from_config(TAVILY)) if config.tools else ([], {})
        )
        self._max_context = None  # model context window, fetched lazily from the provider
        self.open_chat(chat_id)
        logger.debug(
            "initialized agent user=%s agent=%s provider=%s model=%s",
            config.user_id, config.id, config.provider_name, config.models.chat,
        )

    def open_chat(self, chat_id: Optional[str] = None) -> str:
        """Switch to a chat (a new empty one when None), rebuilding the context from its history."""
        conversations = self.sources.conversations()
        if chat_id is None:
            chat_id = conversations.create()
        elif not conversations.exists_chat(chat_id):
            raise FileNotFoundError(f"chat not found: {chat_id}")
        selection = conversations.model_selection(chat_id)
        if selection is not None and selection.provider != self.config.provider_name:
            raise ValueError("this chat's model requires the OpenRouter chat provider")
        model = selection.id if selection is not None else self._default_chat_model
        self.zdr_required = selection.zdr if selection is not None else True
        if model != self.config.models.chat:
            self._max_context = None
        self.config.models = self.config.models.model_copy(update={"chat": model})
        effort = conversations.reasoning_effort_selection(chat_id)
        self.config.reasoning_effort = effort.effort if effort is not None else self._default_reasoning_effort
        self.chat_id = chat_id
        # real input tokens of the current context (seeded on load, updated each turn)
        self._ctx_input_tokens: Optional[int] = None
        # previous usage breakdown, to compute per-category turn deltas
        self._prev_breakdown: Optional[dict] = None
        # conversation history is the memory
        self.messages: list[dict] = [
            {"role": "system", "content": self.config.personality_prompt}
        ]
        # whether older turns were dropped from the in-context window
        self._truncated = False
        # transient associative-memory hint, injected into the context of the current turn only
        self._assoc_suggestion: Optional[str] = None
        self._assoc_match_count = 0
        self._assoc_elapsed = 0.0
        self._model_elapsed = 0.0
        self._turn_started_at = 0.0
        # thinking tokens from the last reply, kept apart from the answer
        self.last_reasoning: str = ""
        # tool calls made during the last turn (name, arguments, truncated result, error flag)
        self.last_actions: list[dict] = []
        # formatted usage breakdown of the last reply (for the caller to display)
        self.last_usage: str = ""
        self.last_latency_report: str = ""
        self.last_reply: str = ""
        self._load_recent_context()
        self._reconstruct_context()
        return chat_id

    def refresh_user_models(self, models: ModelsConfig) -> None:
        """Refresh inherited inference defaults without rebuilding the embedding index."""
        self._default_chat_model = models.chat
        selection = self.sources.conversations().model_selection(self.chat_id)
        model = selection.id if selection else models.chat
        if model != self.config.models.chat:
            self._max_context = None
            self._ctx_input_tokens = None
            self._prev_breakdown = None
        self.config.models = self.config.models.model_copy(update={"chat": model, "stt": models.stt})
        self.zdr_required = selection.zdr if selection else True

    @classmethod
    def from_config(
        cls, user_id: str, agent_id: Optional[str] = None, chat_id: Optional[str] = None
    ) -> "Agent":
        config = load_agent(user_id, agent_id)
        provider = get_provider(config.provider_name, api_key=config.api_key)
        logger.debug(
            "loaded agent user=%s agent=%s provider=%s model=%s",
            config.user_id, config.id, config.provider_name, config.models.chat,
        )
        return cls(config, provider, chat_id)

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

    def invoke(
        self, user_input: str, stream: Optional[bool] = None,
        cancellation: CancellationToken | None = None,
        *, continuation: bool = False, attachments: Optional[list[dict]] = None,
    ) -> Union[str, Iterator[tuple[str, str | dict]]]:
        """Send a user message and return the reply, updating conversation memory.

        Returns a string or a generator of (kind, payload) events. Cancellation preserves
        the accepted input and partial reply as an interrupted turn, then raises
        GenerationCancelled. A steering continuation retains the active turn even without memory.
        """
        if stream is None:
            stream = self.config.stream
        attachments = attachments or []
        self._validate_modalities(attachments)
        user_content = self._user_content(user_input, attachments)
        ts = datetime.now().astimezone().isoformat(timespec="seconds")
        logger.info("invoke user=%s agent=%s stream=%s", self.config.user_id, self.config.id, stream)
        self._turn_started_at = perf_counter()
        # without memory, each turn starts fresh from the system prompt
        if not self.config.has_memory and not continuation:
            self.messages = [{"role": "system", "content": self.config.personality_prompt}]
            self._truncated = False
        # associative memory: run before logging the turn so the search can't match this message
        self._assoc_suggestion = None
        self._assoc_match_count = 0
        self._assoc_elapsed = 0.0
        self._model_elapsed = 0.0
        self.last_actions = []
        self.last_reasoning = ""
        self.last_usage = ""
        self.last_latency_report = ""
        self.last_reply = ""
        try:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            if self.config.has_memory and self.config.associative_memory:
                assoc_started_at = perf_counter()
                self._assoc_suggestion, self._assoc_match_count = associative.recall(
                    self, user_input, cancellation
                )
                self._assoc_elapsed = perf_counter() - assoc_started_at
            if cancellation is not None:
                cancellation.raise_if_cancelled()
        except GenerationCancelled:
            self.messages.append({"role": "user", "content": self._stamped(user_content, ts)})
            self._log_turn("user", user_content, ts=ts)
            if not continuation:
                self._trim_context()
            self._remember_interrupted("", "")
            raise
        self.messages.append({"role": "user", "content": self._stamped(user_content, ts)})
        self._log_turn("user", user_content, ts=ts)
        if not continuation:
            self._trim_context()
        if stream:
            return self._stream(cancellation)
        if cancellation is not None:
            # Keep internal streaming for cancellation and partial history, regardless of channel.
            for _ in self._stream(cancellation):
                pass
            return self.messages[-1]["content"]
        return self._complete()

    def _validate_modalities(self, attachments: list[dict]) -> None:
        """Reject attachments unless the selected provider explicitly advertises support."""
        if not attachments:
            return
        supported = self.provider.supported_modalities(self.config.models.chat)
        for attachment in attachments:
            modality = attachment["modality"]
            if modality not in supported:
                raise UnsupportedModalityError(
                    unsupported_modality_message(self.config.language, modality)
                )

    @staticmethod
    def _user_content(text: str, attachments: list[dict]) -> str | list[dict]:
        """Build the OpenAI-compatible content parts for a user turn."""
        if not attachments:
            return text
        if any(attachment["modality"] == "video" for attachment in attachments):
            text = f"[VIDEO] {text}" if text else "[VIDEO]"
        parts: list[dict] = [{"type": "text", "text": text}] if text else []
        for attachment in attachments:
            modality = attachment["modality"]
            data_url = attachment["data_url"]
            if modality == "image":
                parts.append({"type": "image_url", "image_url": {"url": data_url}})
            elif modality == "audio":
                parts.append({
                    "type": "input_audio",
                    "input_audio": {
                        "data": data_url.split(",", 1)[1],
                        "format": attachment["format"],
                    },
                })
            elif modality == "video":
                parts.append({"type": "video_url", "video_url": {"url": data_url}})
        return parts

    @staticmethod
    def _stamped(content: str | list[dict], ts: str) -> str | list[dict]:
        """Prefix a user turn with its local timestamp so the model knows when it was sent."""
        prefix = f"[{datetime.fromisoformat(ts).isoformat(sep=' ', timespec='minutes')}]"
        if isinstance(content, str):
            return f"{prefix} {content}" if content else prefix
        parts = list(content)
        if parts and parts[0].get("type") == "text":
            parts[0] = {"type": "text", "text": f"{prefix} {parts[0]['text']}".rstrip()}
        else:
            parts.insert(0, {"type": "text", "text": prefix})
        return parts

    def _stream(self, cancellation: CancellationToken | None = None) -> Iterator[tuple[str, str | dict]]:
        cancellation = cancellation or CancellationToken()
        reply, reasoning = "", ""
        try:
            with closing(self._stream_reply(cancellation)) as events:
                for kind, chunk in events:
                    if kind == "content" and isinstance(chunk, str):
                        reply += chunk
                    elif kind == "reasoning" and isinstance(chunk, str):
                        reasoning += chunk
                    yield kind, chunk
        except GenerationCancelled:
            self._remember_interrupted(reply, reasoning)
            raise

    def _stream_reply(self, cancellation: CancellationToken) -> Iterator[tuple]:
        for hop in range(AGENT_MAX_TOOL_HOPS + 1):
            cancellation.raise_if_cancelled()
            is_last = hop == AGENT_MAX_TOOL_HOPS
            tools = None if is_last else (self._tool_specs or None)
            context = self._context()
            if is_last:
                context = context + [{"role": "system", "content": _FINAL_NUDGE}]
            reasoning, reply, api_usage, tool_calls = "", "", None, None
            model_started_at = perf_counter()
            with closing(self.provider.invoke(
                context, self.config.models.chat,
                stream=True, reasoning=self.config.reasoning,
                reasoning_tokens=self.config.reasoning_tokens,
                reasoning_effort=self.config.reasoning_effort, tools=tools,
                cancellation=cancellation,
                zdr=self.zdr_required,
            )) as chunks:
                for chunk in chunks:
                    cancellation.raise_if_cancelled()
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
            self._model_elapsed += perf_counter() - model_started_at
            cancellation.raise_if_cancelled()
            if tool_calls and not is_last:
                tool_calls = _dedupe_tool_calls(tool_calls)
                self._remember_tool_calls(reply, reasoning, tool_calls)
                for action in self._run_tools(tool_calls, cancellation):
                    yield ("tool", action)
                continue
            self.last_reasoning = reasoning
            usage_report = self._format_usage_report(context, reply, reasoning, api_usage)
            with cancellation._commit():
                self._remember_reply(reply, reasoning, api_usage, usage_report=usage_report)
            self._track_usage(context, reply, reasoning, api_usage)
            self._record_latency_report()
            return

    def _complete(self) -> str:
        for hop in range(AGENT_MAX_TOOL_HOPS + 1):
            is_last = hop == AGENT_MAX_TOOL_HOPS
            tools = None if is_last else (self._tool_specs or None)
            context = self._context()
            if is_last:
                context = context + [{"role": "system", "content": _FINAL_NUDGE}]
            model_started_at = perf_counter()
            result = self.provider.invoke(
                context, self.config.models.chat,
                stream=False, reasoning=self.config.reasoning,
                reasoning_tokens=self.config.reasoning_tokens,
                reasoning_effort=self.config.reasoning_effort, tools=tools,
                zdr=self.zdr_required,
            )
            self._model_elapsed += perf_counter() - model_started_at
            reasoning = result.get("reasoning") or ""
            reply = result.get("response") or ""
            tool_calls = result.get("tool_calls")
            if tool_calls and not is_last:
                tool_calls = _dedupe_tool_calls(tool_calls)
                self._remember_tool_calls(reply, reasoning, tool_calls)
                self._run_tools(tool_calls)
                continue
            self.last_reasoning = reasoning
            usage_report = self._format_usage_report(
                context, reply, reasoning, result.get("usage")
            )
            self._remember_reply(
                reply, reasoning, result.get("usage"), usage_report=usage_report
            )
            self._track_usage(context, reply, reasoning, result.get("usage"))
            self._record_latency_report()
            return reply
        return ""  # unreachable: the final hop runs without tools and finalizes

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

    def _run_tools(
        self, tool_calls: list[dict], cancellation: CancellationToken | None = None,
    ) -> list[dict]:
        """Execute each requested tool, append its result to the context and return the actions taken."""
        actions = []
        for tc in tool_calls:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            name = tc["name"]
            args = tc["arguments"]
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {}
            handler = self._tool_handlers.get(name)
            failed = False
            if handler is None:
                result, failed = f"error: unknown tool '{name}'", True
            else:
                try:
                    result = handler(**args)
                except GenerationCancelled:
                    raise
                except Exception as e:  # surfaced back to the model, not fatal
                    result, failed = f"error: {e}", True
            logger.info("tool %s(%s)", name, args)
            self.messages.append(
                {"role": "tool", "tool_call_id": tc["id"], "name": name, "content": result}
            )
            text = str(result)
            if len(text) > _ACTION_RESULT_CHARS:
                text = text[:_ACTION_RESULT_CHARS] + "…"
            actions.append({"name": name, "arguments": args, "result": text, "error": failed})
            self.last_actions.append(actions[-1])
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        return actions

    def _remember_interrupted(self, reply: str, reasoning: str) -> None:
        pending: dict[str, dict] = {}
        for message in self.messages:
            for call in message.get("tool_calls", []):
                pending[call["id"]] = call
            if message.get("role") == "tool":
                pending.pop(message["tool_call_id"], None)
        for call in pending.values():
            self.messages.append({
                "role": "tool", "tool_call_id": call["id"],
                "name": call["function"]["name"],
                "content": "not executed: generation interrupted by user",
            })
        self.last_reasoning = reasoning
        self.last_usage = ""
        self._remember_reply(reply, reasoning, None, interrupted=True)

    def _remember_reply(
        self, reply: str, reasoning: str, api_usage, interrupted: bool = False,
        usage_report: Optional[str] = None,
    ) -> None:
        """Append the assistant reply to memory and persist the turn (with its real usage)."""
        message = {"role": "assistant", "content": self._reply_content(reply, interrupted)}
        if self.config.keep_reasoning_in_memory and reasoning:
            message["reasoning"] = reasoning
        self.messages.append(message)
        self.last_reply = reply
        extra = {
            "model": self.config.models.chat,
            "zdr": self.zdr_required,
            "reasoning": reasoning or None,
            "actions": self.last_actions or None,
            "associative_matches": self._assoc_match_count or None,
            "usage_report": usage_report or self.last_usage or None,
        }
        if interrupted:
            extra["interrupted"] = True
        if api_usage:
            extra["usage"] = {"in": api_usage.get("prompt_tokens"), "out": api_usage.get("completion_tokens")}
        self._log_turn("assistant", reply, **extra)

    @staticmethod
    def _reply_content(reply: str | list[dict], interrupted: bool) -> str | list[dict]:
        if not interrupted:
            return reply
        if isinstance(reply, list):
            return [*reply, {"type": "text", "text": _INTERRUPTED_MARKER}]
        return f"{reply}\n\n{_INTERRUPTED_MARKER}"

    def _log_turn(self, role: str, content: str | list[dict], **extra) -> None:
        """Persist a turn to the active chat's jsonl (independent of in-context memory)."""
        extra = {k: v for k, v in extra.items() if v is not None}
        self.sources.conversations().append(self.chat_id, role, content, **extra)

    # ------------------------------------------------------------------ context window

    def _load_recent_context(self) -> None:
        """Seed the context with the last N turns of the active chat (all of them when N is unset)."""
        if not self.config.has_memory:
            return
        history = self.sources.conversations().load(self.chat_id)
        n = self.config.conversation_messages_retained
        truncated = bool(n) and len(history) > n
        if n:
            history = history[-n:]
        for t in history:
            if t.get("role") not in ("user", "assistant"):
                continue
            content = self._reply_content(t["content"], bool(t.get("interrupted")))
            if t["role"] == "user" and t.get("ts"):
                content = self._stamped(content, t["ts"])
            self.messages.append({"role": t["role"], "content": content})
        # seed the context input size from the most recent persisted usage
        for t in reversed(history):
            u = t.get("usage")
            if u and u.get("in") is not None:
                if t.get("model", self._default_chat_model) == self.config.models.chat:
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
        ctx = self.messages
        if self._truncated:
            ctx = [self.messages[0], {"role": "system", "content": "[CUTOFF CONVERSATION]"}] + self.messages[1:]
        if self._assoc_suggestion:
            ctx = ctx + [{"role": "system", "content": self._assoc_suggestion}]
        return ctx

    def _ensure_max_context(self) -> None:
        if self._max_context is None:
            self._max_context = self.provider.context_window(self.config.models.chat) or 0

    def _format_usage_report(
        self, context: list[dict], reply: str, reasoning: str, api_usage
    ) -> str:
        """Format a history-safe usage report without delaying the reply commit."""
        api_usage = api_usage or {}
        breakdown, approx = usage.turn_breakdown(
            context,
            reply,
            reasoning,
            api_usage.get("prompt_tokens"),
            api_usage.get("completion_tokens"),
            self._tool_specs or None,
        )
        return usage.format_turn(
            breakdown, self._prev_breakdown, approx, self._max_context or None
        )

    def _record_latency_report(self) -> None:
        """Format ephemeral wall-clock timings for the latest completed response."""
        total = max(0.0, perf_counter() - self._turn_started_at)
        assoc = self._assoc_elapsed
        model = self._model_elapsed
        other = max(0.0, total - assoc - model)

        def part(name: str, seconds: float) -> str:
            percentage = seconds / total * 100 if total else 0.0
            return f"  {name}: {seconds * 1000:.0f} ms ({percentage:.1f}%)"

        self.last_latency_report = "\n".join(
            [
                f"latency: total={total * 1000:.0f} ms",
                part("associative memory", assoc),
                part("model calls", model),
                part("tools/other", other),
            ]
        )

    def _reconstruct_context(self) -> None:
        """On load, show how loaded the context is, using the last persisted real input tokens."""
        if self._ctx_input_tokens is None:
            return  # nothing loaded / no real data yet; wait for the first turn
        breakdown, approx = usage.context_breakdown(self.messages, self._ctx_input_tokens, self._tool_specs or None)
        self._ensure_max_context()
        self._prev_breakdown = breakdown
        self.last_usage = usage.format_context(breakdown, approx, self._max_context or None)
        logger.info("usage\n%s", self.last_usage)

    def _track_usage(self, context: list[dict], reply: str, reasoning: str, api_usage) -> None:
        """Split this turn's real in/out tokens and record how loaded the context now is."""
        api_usage = api_usage or {}
        prompt_tokens = api_usage.get("prompt_tokens")
        completion_tokens = api_usage.get("completion_tokens")
        breakdown, approx = usage.turn_breakdown(context, reply, reasoning, prompt_tokens, completion_tokens, self._tool_specs or None)
        if approx:
            logger.warning("token usage approximated with chars/4 (provider returned no usage)")
        self._ensure_max_context()
        self.last_usage = usage.format_turn(breakdown, self._prev_breakdown, approx, self._max_context or None)
        self._prev_breakdown = breakdown
        self._ctx_input_tokens = breakdown["in"]  # for the next reconstruct
        logger.info("usage\n%s", self.last_usage)
