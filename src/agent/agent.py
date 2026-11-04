from __future__ import annotations

import json
import threading
from datetime import datetime
from time import perf_counter
from contextlib import ExitStack, closing
from typing import Iterator, Optional, Union

import src.providers.openrouter  # noqa: F401  (registers the openrouter provider)
import src.providers.ollama  # noqa: F401  (registers the ollama provider)
from src.config.model_refs import CLAUDE_SUBSCRIPTION, prefix_of, provider_of
from src.providers.routing import provider_for
from src.agent import usage
from src.agent import associative
from src.agent.profile import AgentProfile, load_profile, system_prompt
from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS
from src.agent.tools import build_tools, chat_folder_options
from src.agent.toolsets.mcp import build_mcp_tools
from src.config.loader import load_agent
from src.config.models import ModelsConfig, ResolvedAgent
from src.config.settings import AGENT_MAX_TOOL_HOPS, PROMPT_CACHE, TAVILY
from src.config.zdr import chat_zdr
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
from src.providers.mcp import McpManager
from src.providers.tavily import TavilyClient

logger = get_logger("agent")

# appended to the context on the final tool hop to force a direct answer
_FINAL_NUDGE = "Stop calling tools. Answer the user now using what you already have."
# max characters of a tool result shown to channels and stored with the turn
_ACTION_RESULT_CHARS = 500
_INTERRUPTED_MARKER = "[Response interrupted by user.]"
# separates the text written before an associative-memory hit interrupted the reply from its continuation
_HIT_BREAK = "\n\n"
_ASSOC_NOTICE = (
    "## Associative memory\n\n"
    "While you write, the system searches your long-term memory in parallel. If it finds something relevant, "
    "your reply is paused and a system message starting with [associative memory] is added after the text you "
    "had written so far. Treat it as a hint: continue from where you stopped, correct yourself if the fragments "
    "show you were wrong, and do not repeat what you already wrote."
)
_HIT_CUT_NOTE = (
    "Your reply above was paused at this point so this could be added. Continue it from exactly where it "
    "stopped, without repeating anything (if it stopped mid-sentence, finish that sentence directly, with "
    "no ellipsis). If the fragments contradict or fill in what you already wrote, say so and correct it."
)


_DISPOSABLE_NOTICE = (
    "## Disposable chat\n\n"
    "This is a disposable chat: nothing from it will be remembered. Your persistent memory, SOUL, skills, "
    "notes and linked folders are read-only here, so you cannot change them. If the user wants something "
    "remembered, tell them they can convert this chat to a normal one."
)


def _ends_sentence(text: str) -> bool:
    """Whether `text` stops at a point where a reply can be paused without cutting a sentence."""
    return text.rstrip()[-1:] in (".", "!", "?") or text.endswith("\n")


def _continuation_gap(partial: str) -> str:
    """What joins a paused reply to its continuation so the combined text reads naturally."""
    return _HIT_BREAK if partial.rstrip()[-1:] in (".", "!", "?") and not partial.endswith("\n") else ""


def _union_seconds(spans: list[tuple[float, float]]) -> float:
    """Total time covered by possibly overlapping (start, end) spans."""
    total, reach = 0.0, None
    for start, end in sorted(spans):
        if reach is None or start > reach:
            total += end - start
            reach = end
        elif end > reach:
            total += end - reach
            reach = end
    return total


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


def provider_settings(config: ResolvedAgent) -> dict:
    """The user's provider settings, as `provider_for` takes them."""
    return {
        "main": config.provider_name, "main_api_key": config.api_key, "openrouter": config.openrouter,
        "ollama": config.ollama, "claude": config.claude_subscription, "user_id": config.user_id,
    }


def named_provider(config: ResolvedAgent, name: str, purpose: str) -> LLMProvider:
    """The provider `name` with this user's settings; raises ProviderUnavailable when it cannot be used."""
    return provider_for(name, purpose=purpose, **provider_settings(config))


def main_provider(config: ResolvedAgent) -> LLMProvider:
    """The user's main provider, which serves bare model ids."""
    return named_provider(config, config.provider_name, "the main provider")


def embeddings_provider(config: ResolvedAgent, chat_provider: LLMProvider) -> LLMProvider:
    """The provider used for embeddings: a `<provider>:` prefix, else `embeddings_provider`, else the main one."""
    named = prefix_of(config.models.embeddings)
    if named is not None:
        return named_provider(config, named, "models.embeddings")
    if config.embeddings_provider_name == config.provider_name and config.embeddings_api_key == config.api_key:
        return chat_provider
    return get_provider(config.embeddings_provider_name, api_key=config.embeddings_api_key)


def model_provider(config: ResolvedAgent, model: str) -> LLMProvider:
    """The provider serving a model reference: its `<provider>:` prefix, else the user's main provider."""
    return named_provider(config, provider_of(model, config.provider_name), f"the model {model}")


class Agent:
    """Orchestration entry point: wraps an agent's config, provider and conversation memory."""

    def __init__(
        self, config: ResolvedAgent, provider: LLMProvider, chat_id: Optional[str] = None,
        mcp: McpManager | None = None, *, sources: SourceRegistry | None = None,
        profile: AgentProfile | None = None,
    ):
        self.config = config.model_copy(deep=True)
        self._default_chat_model = config.models.chat
        self._default_reasoning_effort = config.reasoning_effort
        self.provider = provider
        # the user's main provider (embeddings, bare model ids); a `<provider>:` model switches `provider`
        self._base_provider = provider
        self._providers: dict[str, LLMProvider] = {config.provider_name: provider}
        self._mcp = mcp
        # per-agent access to the data plane (mind palace, conversations, own files, user folders)
        self.sources = sources or SourceRegistry.for_agent(config.user_id, config.id, **chat_folder_options(config))
        # SOUL, memories, skills index and questions, frozen for this chat's lifetime
        self.profile = profile or AgentProfile(limits=config.memory_limits)
        # semantic index over the indexed sources; updated in the background, never on a turn's path
        self.index = VectorIndex.for_agent(
            config.user_id, config.id, self.sources,
            self._embeddings_provider(), config.models.embeddings,
        )
        self.index.reconcile()  # cheap stat-only scan, queued; user folders only when their interval passed
        # addresses the tools of the current turn wrote, indexed with the turn's exchange after the reply
        self._written: set[str] = set()
        # a disposable chat (decided by its stored flag) cannot change the agent's persistent state
        self._disposable = chat_id is not None and self.sources.conversations().is_disposable(chat_id)
        self._base_tool_specs, self._base_tool_handlers = self._build_base_tools()
        self._tool_specs, self._tool_handlers = list(self._base_tool_specs), dict(self._base_tool_handlers)
        # MCP notes appended to the system prompt; refreshed with the MCP tools at each turn
        self._mcp_notes: list[str] = []
        self._max_context = None  # model context window, fetched lazily from the provider
        self.open_chat(chat_id)
        logger.debug(
            "initialized agent user=%s agent=%s provider=%s model=%s",
            config.user_id, config.id, config.provider_name, config.models.chat,
        )

    def _build_base_tools(self) -> tuple[list[dict], dict]:
        config = self.config
        if not (config.tools and (config.tool_groups or self.profile.skills)):
            return [], {}
        return build_tools(
            self, TavilyClient.from_config(TAVILY), config.tool_groups, config.allow, config.deny,
            skill_view=bool(self.profile.skills), disposable=self._disposable,
        )

    def open_chat(self, chat_id: Optional[str] = None) -> str:
        """Switch to a chat (a new empty one when None), rebuilding the context from its history."""
        conversations = self.sources.conversations()
        if chat_id is None:
            chat_id = conversations.create()
        elif not conversations.exists_chat(chat_id):
            raise FileNotFoundError(f"chat not found: {chat_id}")
        disposable = conversations.is_disposable(chat_id)
        if disposable != self._disposable:  # another chat kind: rebuild the tools (the prompt is rebuilt below)
            self._disposable = disposable
            self._base_tool_specs, self._base_tool_handlers = self._build_base_tools()
            self._tool_specs, self._tool_handlers = list(self._base_tool_specs), dict(self._base_tool_handlers)
        selection = conversations.model_selection(chat_id)
        model = selection.id if selection is not None else self._default_chat_model
        self.zdr_required = chat_zdr(self.config.require_zdr, model, selection.zdr if selection is not None else None)
        if model != self.config.models.chat:
            self._max_context = None
        self._route_provider(model)
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
            {"role": "system", "content": self._system_prompt()}
        ]
        # whether older turns were dropped from the in-context window
        self._truncated = False
        # transient associative-memory hint, injected into the context of the current turn only
        self._assoc_suggestion: Optional[str] = None
        self._assoc_match_count = 0
        self._assoc_job: Optional[associative.RecallJob] = None
        self._assoc_taken = False  # the job's outcome was already used (or found empty) this turn
        # how the search ended: "found", "none" (nothing relevant) or "skipped" (failed, late or interrupted)
        self._assoc_outcome: Optional[str] = None
        self._assoc_parallel = False
        self._assoc_span: Optional[tuple[float, float]] = None
        self._model_spans: list[tuple[float, float]] = []
        # reply text written before a memory hit paused it, and the reasoning that came with it
        self._partial = ""
        self._carried_reasoning = ""
        self._turn_started_at = 0.0
        # thinking tokens from the last reply, kept apart from the answer
        self.last_reasoning: str = ""
        # tool calls made during the last turn (name, arguments, truncated result, error flag)
        self.last_actions: list[dict] = []
        # formatted usage breakdown of the last reply (for the caller to display)
        self.last_usage: str = ""
        self.last_latency_report: str = ""
        self.last_reply: str = ""
        # the user's own cap on associative fragments per turn (None = application default)
        self.associative_max_matches: int | None = None
        self._load_recent_context()
        self._reconstruct_context()
        return chat_id

    def refresh_user_models(self, models: ModelsConfig, associative_max_matches: int | None = None) -> None:
        """Refresh inherited inference defaults and the user's associative cap without rebuilding the index."""
        self.associative_max_matches = associative_max_matches
        self._default_chat_model = models.chat
        selection = self.sources.conversations().model_selection(self.chat_id)
        model = selection.id if selection else models.chat
        if model != self.config.models.chat:
            self._max_context = None
            self._ctx_input_tokens = None
            self._prev_breakdown = None
        self._route_provider(model)
        self.config.models = self.config.models.model_copy(update={"chat": model, "stt": models.stt})
        self.zdr_required = chat_zdr(self.config.require_zdr, model, selection.zdr if selection else None)

    @classmethod
    def from_config(
        cls, user_id: str, agent_id: Optional[str] = None, chat_id: Optional[str] = None,
        mcp: McpManager | None = None,
    ) -> "Agent":
        # an agent default the ZDR policy forbids is checked per chat by the service, where a chat's own model wins
        config = load_agent(user_id, agent_id, check_agent_model=False)
        provider = main_provider(config)
        sources = SourceRegistry.for_agent(config.user_id, config.id, **chat_folder_options(config))
        profile = load_profile(sources, config.memory_limits)
        logger.debug(
            "loaded agent user=%s agent=%s provider=%s model=%s",
            config.user_id, config.id, config.provider_name, config.models.chat,
        )
        return cls(config, provider, chat_id, mcp, sources=sources, profile=profile)

    @property
    def name(self) -> str:
        return self.config.name

    def _associative_enabled(self) -> bool:
        """Whether this agent searches its memory each turn."""
        return self.config.has_memory and self.config.associative_memory != "off"

    def _associative_parallel(self) -> bool:
        """Whether the search may run beside the model and pause its reply."""
        return self._associative_enabled() and self.config.associative_execution == "parallel"

    def _system_prompt(self) -> str:
        """SOUL and profile blocks, then the associative-memory notice and MCP notes."""
        blocks = []
        if self._associative_parallel():
            blocks.append(_ASSOC_NOTICE)
        if self._disposable:
            blocks.append(_DISPOSABLE_NOTICE)
        if self._mcp_notes:
            blocks.append("## Connected services (MCP)\n\n" + "\n\n".join(self._mcp_notes))
        return system_prompt(self.profile, blocks)

    def _refresh_mcp_tools(self) -> None:
        """Rebuild this turn's MCP tools, so connecting or disconnecting a service applies at once."""
        if self._mcp is None or not self.config.tools or not self.config.mcp:
            return
        toolset = build_mcp_tools(self._mcp, self.config.user_id, self.config.mcp)
        self._tool_specs = [*self._base_tool_specs, *toolset.specs]
        self._tool_handlers = {**self._base_tool_handlers, **toolset.handlers}
        self._mcp_notes = toolset.notes
        self.messages[0] = {"role": "system", "content": self._system_prompt()}

    def _embeddings_provider(self) -> LLMProvider:
        return embeddings_provider(self.config, self._base_provider)

    def provider_named(self, name: str, purpose: str = "this chat's model") -> LLMProvider:
        """One of the user's providers (created on first use); raises ProviderUnavailable when it cannot be used."""
        if name not in self._providers:
            self._providers[name] = named_provider(self.config, name, purpose)
        return self._providers[name]

    def claude_provider(self, purpose: str = "this chat's model") -> LLMProvider:
        """The user's Claude subscription provider (Claude Code reports a missing install or login when it runs)."""
        return self.provider_named(CLAUDE_SUBSCRIPTION, purpose)

    def _route_provider(self, model: str) -> None:
        """Serve a model reference with its provider: the `<provider>:` prefix, else the main provider."""
        self.provider = self.provider_named(provider_of(model, self.config.provider_name), f"the model {model}")

    def query_memory(self, query: str, top_k: int = 5, *, exclude_chat: str | None = None) -> list[dict]:
        """Semantic recall over what is indexed (never syncs); `exclude_chat` leaves one chat's chunks out."""
        queried_at = perf_counter()
        results = self.index.query(query, top_k, exclude_chat=exclude_chat)
        logger.debug(
            "query_memory %r -> %d hits (%.0f ms)", query, len(results), (perf_counter() - queried_at) * 1000,
        )
        return results

    def note_written(self, address: str) -> None:
        """Record that a tool changed `address`; it is reindexed in the background after the reply."""
        self._written.add(address)

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
            self.messages = [{"role": "system", "content": self._system_prompt()}]
            self._truncated = False
        self._assoc_suggestion = None
        self._assoc_match_count = 0
        self._assoc_job = None
        self._assoc_taken = False
        self._assoc_outcome = None
        self._assoc_parallel = False
        self._assoc_span = None
        self._model_spans = []
        self._partial = ""
        self._carried_reasoning = ""
        self.last_actions = []
        self.last_reasoning = ""
        self.last_usage = ""
        self.last_latency_report = ""
        self.last_reply = ""
        try:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
        except GenerationCancelled:
            self.messages.append({"role": "user", "content": self._stamped(user_content, ts)})
            self._log_turn("user", user_content, ts=ts)
            if not continuation:
                self._trim_context()
            self._remember_interrupted("", "")
            raise
        if self._associative_enabled():
            # the search runs beside the model call and sees the history without this message
            self._assoc_parallel = self._associative_parallel() and (stream or cancellation is not None)
            self._assoc_job = associative.RecallJob(
                self, list(self.messages[1:]), user_input, chat_id=self.chat_id,
            ).start()
        self.messages.append({"role": "user", "content": self._stamped(user_content, ts)})
        self._log_turn("user", user_content, ts=ts)
        if not continuation:
            self._trim_context()
        self._refresh_mcp_tools()
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
            self._partial = ""  # `reply` already holds everything that was shown
            self._close_assoc_job()
            self._remember_interrupted(reply, reasoning)
            raise
        finally:
            self._close_assoc_job()

    def _stream_reply(self, cancellation: CancellationToken) -> Iterator[tuple]:
        if self._assoc_job is not None and not self._assoc_taken:
            yield ("memory", {"state": "searching"})
            if not self._assoc_parallel:  # sequential: the model starts once the search has finished
                while not self._assoc_job.wait(0.05):
                    cancellation.raise_if_cancelled()
                yield from self._take_assoc_result()
        for hop in range(AGENT_MAX_TOOL_HOPS + 1):
            cancellation.raise_if_cancelled()
            is_last = hop == AGENT_MAX_TOOL_HOPS
            tools = None if is_last else (self._tool_specs or None)
            while True:  # runs again when a memory hit pauses the call
                yield from self._take_assoc_result()
                context = self._context()
                if is_last:
                    context = context + [{"role": "system", "content": _FINAL_NUDGE}]
                reasoning, reply, api_usage, tool_calls = "", "", None, None
                job = None if self._assoc_taken else self._assoc_job
                call_token = CancellationToken() if job is not None else cancellation
                paused = False
                cut_ok = threading.Event()  # set while the text written so far ends a sentence (or is empty)
                cut_ok.set()
                model_started_at = perf_counter()
                try:
                    with ExitStack() as stack:
                        if job is not None:
                            # the job cancels only this call (not the turn) the moment it finds something
                            stack.enter_context(cancellation._link(call_token))
                            job.attach(call_token, cut_ok)
                            stack.callback(job.detach)
                        chunks = stack.enter_context(closing(self.provider.invoke(
                            context, self.config.models.chat,
                            stream=True, reasoning=self.config.reasoning,
                            reasoning_tokens=self.config.reasoning_tokens,
                            reasoning_effort=self.config.reasoning_effort, tools=tools,
                            cancellation=call_token,
                            zdr=self.zdr_required,
                        )))
                        for chunk in chunks:
                            call_token.raise_if_cancelled()
                            if isinstance(chunk, tuple):
                                kind, text = chunk
                                if kind == "reasoning":
                                    reasoning += text
                                else:
                                    reply += text
                                    cut_ok.set() if _ends_sentence(reply) else cut_ok.clear()
                                yield chunk
                                if job is not None and job.ready() and not job.has_hit():
                                    yield from self._take_assoc_result()  # nothing to add: settle the indicator now
                                if job is not None and cut_ok.is_set() and job.has_hit():
                                    paused = True  # the hit arrived while mid-sentence; this is the next sentence end
                                    break
                            elif isinstance(chunk, dict):
                                api_usage = chunk.get("usage")
                                tool_calls = chunk.get("tool_calls")
                except GenerationCancelled:
                    cancellation.raise_if_cancelled()  # an interrupt from the user ends the turn
                    if job is None or not job.ready():
                        raise
                    paused = True
                self._model_spans.append((model_started_at, perf_counter()))
                if not paused:
                    break
                partial = reply
                logger.info(
                    "memory hit paused the reply after %.0f ms (%d chars written); continuing with the hint",
                    (perf_counter() - self._turn_started_at) * 1000, len(partial),
                )
                yield from self._take_assoc_result()
                self._carried_reasoning += reasoning
                if partial.strip():
                    self._partial = partial
                    if gap := _continuation_gap(partial):
                        yield ("content", gap)
            cancellation.raise_if_cancelled()
            reasoning = self._carried_reasoning + reasoning
            if self._partial:
                reply = f"{self._partial}{_continuation_gap(self._partial)}{reply}"
            self._partial = self._carried_reasoning = ""
            if tool_calls and not is_last:
                tool_calls = _dedupe_tool_calls(tool_calls)
                self._remember_tool_calls(reply, reasoning, tool_calls)
                for action in self._run_tools(tool_calls, cancellation):
                    yield ("tool", action)
                continue
            self.last_reasoning = reasoning
            unused = self._assoc_job is not None and not self._assoc_taken
            self._close_assoc_job()  # a hit that arrives after the reply is complete is not used
            if unused:
                yield ("memory", self.memory_event())
            usage_report = self._format_usage_report(context, reply, reasoning, api_usage)
            with cancellation._commit():
                self._remember_reply(reply, reasoning, api_usage, usage_report=usage_report)
            self._track_usage(context, reply, reasoning, api_usage)
            self._record_latency_report()
            return

    def _take_assoc_result(self) -> Iterator[tuple[str, dict]]:
        """Use the finished recall job's hint (once per turn), yielding a memory event for the channels."""
        job = self._assoc_job
        if job is None or self._assoc_taken or not job.ready():
            return
        self._assoc_taken = True
        suggestion, count = job.result()
        waited = ((job.finished_at or perf_counter()) - job.started_at) * 1000
        if job.failed:
            self._assoc_outcome = "skipped"
        elif not suggestion:
            logger.info("associative recall found nothing (%.0f ms)", waited)
            self._assoc_outcome = "none"
        else:
            logger.info("associative recall ready: %d fragment(s) after %.0f ms", count, waited)
            self._assoc_suggestion, self._assoc_match_count = suggestion, count
            self._assoc_outcome = "found"
        yield ("memory", self.memory_event())

    def memory_event(self) -> Optional[dict]:
        """Payload of the final `memory` event for the search of this turn, or None when none ran."""
        if self._assoc_outcome == "skipped":
            return {"state": "skipped"}
        if self._assoc_outcome is None:
            return None
        return {"state": "done", "matches": self._assoc_match_count}

    def _close_assoc_job(self) -> None:
        """Stop the recall job if it is still running and remember how long it ran."""
        job = self._assoc_job
        if job is None:
            return
        self._assoc_job = None
        if not self._assoc_taken:
            if job.ready() and not job.failed and not job.result()[0]:
                self._assoc_outcome = "none"
            else:
                self._assoc_outcome = "skipped"  # failed, late or interrupted: its result was never used
                logger.info("associative recall not used: %s", "still running" if not job.ready() else "arrived too late")
        job.cancel()
        self._assoc_span = (job.started_at, job.finished_at or perf_counter())

    def _complete(self) -> str:
        if self._assoc_job is not None:  # no way to pause a reply here, so the search finishes first
            self._assoc_job.wait()
            for _ in self._take_assoc_result():
                pass
            self._close_assoc_job()
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
            self._model_spans.append((model_started_at, perf_counter()))
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
            if self._disposable and name in DISPOSABLE_BLOCKED_TOOLS:  # defence in depth: the specs already omit these
                result, failed = f"error: '{name}' is not available in a disposable chat", True
            elif handler is None:
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
            "associative_matches": self._assoc_match_count if self._assoc_outcome in ("found", "none") else None,
            "associative_skipped": True if self._assoc_outcome == "skipped" else None,
            "usage_report": usage_report or self.last_usage or None,
        }
        if interrupted:
            extra["interrupted"] = True
        if api_usage:
            extra["usage"] = {"in": api_usage.get("prompt_tokens"), "out": api_usage.get("completion_tokens")}
            if (cache := usage.cache_tokens(api_usage)) is not None:
                extra["usage"].update(cache_read=cache[0], cache_write=cache[1])
        self._log_turn("assistant", reply, **extra)
        written, self._written = self._written, set()
        self.index.after_turn(self.chat_id, written)  # queued: the reply is not delayed

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
        """Keep at most the last N messages (system prompt aside), flagging that older ones were cut.

        A cut moves the start of the history, which invalidates the provider's prompt cache for all of it, so
        each cut goes `trim_slack_percent` below N and the next ones wait until the window fills up again.
        """
        n = self.config.conversation_messages_retained
        if not n:
            return
        rest = self.messages[1:]
        if len(rest) > n:
            keep = max(1, n - n * PROMPT_CACHE.trim_slack_percent // 100)
            kept = rest[-keep:]
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
        if self._partial:
            ctx = ctx + [{"role": "assistant", "content": self._partial.rstrip()}]
        if self._assoc_suggestion:
            hint = f"{self._assoc_suggestion}\n\n{_HIT_CUT_NOTE}" if self._partial else self._assoc_suggestion
            ctx = ctx + [{"role": "system", "content": hint}]
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
            breakdown, self._prev_breakdown, approx, self._max_context or None,
            cache=usage.cache_tokens(api_usage),
        ) + usage.subscription_note(api_usage)

    def _record_latency_report(self) -> None:
        """Format ephemeral wall-clock timings for the latest completed response."""
        total = max(0.0, perf_counter() - self._turn_started_at)
        spans = list(self._model_spans)
        assoc = 0.0
        if self._assoc_span is not None:
            assoc = self._assoc_span[1] - self._assoc_span[0]
            spans.append(self._assoc_span)
        model = sum(end - start for start, end in self._model_spans)
        # parallel work counts once toward the turn, so the parts below may add up to more than the total
        other = max(0.0, total - _union_seconds(spans))

        def part(name: str, seconds: float) -> str:
            percentage = seconds / total * 100 if total else 0.0
            return f"  {name}: {seconds * 1000:.0f} ms ({percentage:.1f}%)"

        self.last_latency_report = "\n".join(
            [
                f"latency: total={total * 1000:.0f} ms",
                part("associative memory (parallel)" if self._assoc_parallel else "associative memory", assoc),
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
        self.last_usage = usage.format_turn(
            breakdown, self._prev_breakdown, approx, self._max_context or None,
            cache=usage.cache_tokens(api_usage),
        )
        self._prev_breakdown = breakdown
        self._ctx_input_tokens = breakdown["in"]  # for the next reconstruct
        logger.info("usage\n%s", self.last_usage)
