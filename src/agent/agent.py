from __future__ import annotations

import json
import threading
from datetime import datetime
from time import perf_counter
from contextlib import ExitStack, closing
from typing import Callable, Iterator, Optional, Union

import src.providers.openrouter  # noqa: F401  (registers the openrouter provider)
import src.providers.ollama  # noqa: F401  (registers the ollama provider)
from src.config.model_refs import CLAUDE_SUBSCRIPTION, is_claude_subscription, prefix_of, provider_of
from src.providers.routing import provider_for
from src.agent import usage
from src.agent import associative
from src.agent import compaction
from src.agent.permissions_prompt import block_for
from src.agent.profile import AgentProfile, load_profile, system_prompt
from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_TOOLS
from src.agent.tools import TOOL_ORDER, build_tools, chat_folder_options
from src.agent.toolsets.delegate import delegate_notice, worker_prompt
from src.agent.toolsets.agents import agents_notice
from src.agent.toolsets.permissions import permissions_notice
from src.agent.toolsets.terminal import terminal_notice
from src.agent.toolsets.mcp import build_mcp_tools
from src.config.ceiling import refusal as level_refusal, restrict_config, tool_allowed
from src.config.loader import load_agent
from src.config.models import ModelsConfig, ResolvedAgent
from src.config.settings import AGENT_MAX_TOOL_HOPS, TAVILY
from src.config.zdr import chat_zdr
from src.core.index import VectorIndex
from src.core.sources.conversations import ChatTask
from src.core.timeline import TimelineBuilder
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
_CHANNEL_NAMES = {"web": "web UI", "telegram": "telegram"}  # how a message channel reads in the model context

# appended to the context on the final tool hop to force a direct answer
_FINAL_NUDGE = "Stop calling tools. Answer the user now using what you already have."
# rounds left (this one included) at which the model is warned that the tool budget is about to run out
_HOPS_WARNING_AT = 2


def _hops_warning(hop: int, limit: int) -> str | None:
    """System note for the model when `_HOPS_WARNING_AT` tool rounds remain in the turn, else None."""
    left = limit - hop
    if left != _HOPS_WARNING_AT or limit <= _HOPS_WARNING_AT:
        return None
    return (
        f"Tool budget: {left} tool rounds left in this turn (this one included, {hop} used of {limit}). "
        "After that you must answer without tools. Finish what matters now and say what is still pending."
    )
# max characters of a tool result shown to channels and stored with the turn
_ACTION_RESULT_CHARS = 500
_INTERRUPTED_MARKER = "[Response interrupted by user.]"
# closes a checkpoint turn that was never replaced by the final one (the server stopped mid-reply)
_CHECKPOINT_MARKER = "[Response cut off: the server stopped before it finished. What is shown above was saved as it happened.]"
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


_DREAM_TASKS_NOTICE = (
    "## Tasks for your dreams\n\n"
    "When the user asks you to do something in a dream (research a topic, improve one of your skills or "
    "notes), add it to `mind_palace:dream_tasks.md` as one `- [ ] task (date, why)` item; your next dream "
    "session does it and checks it off. Tell the user it is queued for the next dream."
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
        # SOUL, memories, skills index and for_user items, frozen for this chat's lifetime
        self.profile = profile or AgentProfile()
        # semantic index over the indexed sources; updated in the background, never on a turn's path
        self.index = VectorIndex.for_agent(
            config.user_id, config.id, self.sources,
            self._embeddings_provider(), config.models.embeddings,
        )
        self.index.reconcile()  # cheap stat-only scan, queued; user folders only when their interval passed
        # addresses the tools of the current turn wrote, indexed with the turn's exchange after the reply
        self._written: set[str] = set()
        # a disposable chat (decided by its stored flag) cannot change the agent's persistent state
        # the hidden chat of a subagent task is also disposable and carries its grant and worker prompt (stored mark)
        self._disposable, self._task = self._chat_kind(chat_id)
        # called with (input tokens, output tokens) after each model call; the task manager's token watchdog
        self.usage_hook: Callable[[int, int], None] | None = None
        if self._task is not None:
            self._become_worker(self._task)
        self._base_tool_specs, self._base_tool_handlers = self._build_base_tools()
        self._tool_specs, self._tool_handlers = list(self._base_tool_specs), dict(self._base_tool_handlers)
        # MCP notes appended to the system prompt; refreshed with the MCP tools at each turn
        self._mcp_notes: list[str] = []
        self._mcp_tool_names: frozenset[str] = frozenset()  # this turn's MCP tools: a class a disposable chat refuses
        self._max_context = None  # model context window, fetched lazily from the provider
        self.open_chat(chat_id)
        logger.debug(
            "initialized agent user=%s agent=%s provider=%s model=%s",
            config.user_id, config.id, config.provider_name, config.models.chat,
        )

    def _chat_kind(self, chat_id: Optional[str]) -> tuple[bool, ChatTask | None]:
        """Whether the chat is disposable and, for a subagent task's chat, its stored mark."""
        if chat_id is None:
            return False, None
        conversations = self.sources.conversations()
        task = conversations.task_mark(chat_id)
        # a `remember` task chat (existing agent writing its own memory) is hidden but ordinary, not disposable
        return (task is not None and not task.remember) or conversations.is_disposable(chat_id), task

    def _become_worker(self, task: ChatTask) -> None:
        """Turn this agent into the ephemeral worker of a task: the parent's prompt, no memory, no MCP, no search."""
        if task.kind == "agent":  # an existing agent keeps its own SOUL, memories, skills and settings (read-only unless `remember`)
            self.config.mcp = {}
            if task.remember:  # it may write its own memory, nothing else about itself
                self.config.deny = [*(self.config.deny or []), "soul_edit", "skill_manage"]
        else:
            self.profile = AgentProfile(soul=worker_prompt(task.instructions))
            self.config.associative_memory = "off"
            self.config.mcp = {}
        cfg = self.config.terminal
        if cfg is not None and "terminal" in task.groups:
            # only the granted backends the parent has, manual approvals unless `inherit` (a child never gets `off`)
            keep = [name for name in task.backends if name in cfg.backends and name in self.config.terminal_backends]
            if keep:
                self.config.terminal = cfg.model_copy(update={
                    "backends": {name: cfg.backends[name].model_copy(update={"approvals": self._worker_approvals(task, cfg.backends[name])}) for name in keep},
                    "default_backend": cfg.default_backend if cfg.default_backend in keep else keep[0],
                })
                self.config.terminal_backends = {name: self.config.terminal_backends[name] for name in keep}
            else:
                self.config.terminal = None
        elif cfg is not None:
            self.config.terminal = None
        # a worker never outranks its launcher: groups, MCP, backends and delegation above the cap are dropped
        self.config = restrict_config(self.config, task.level)

    @staticmethod
    def _worker_approvals(task: ChatTask, backend) -> str:
        """Approval mode of a worker's backend: `manual`, except an unattended task keeps `smart` (nobody is asked
        anyway: dangerous and manual actions are denied, safe ones run) and an `inherit` worker keeps its parent's mode;
        `off` is never inherited."""
        if backend.approvals == "off":
            return "manual"
        if task.unattended and backend.approvals == "smart" or task.inherit:
            return backend.approvals
        return "manual"

    def _build_base_tools(self) -> tuple[list[dict], dict]:
        config = self.config
        groups = config.tool_groups
        if self._task is not None:  # only the groups the parent granted, and only if this agent has them too
            groups = [group for group in (groups or []) if group in self._task.groups]
        if not (config.tools and (groups or self.profile.skills)):
            return [], {}
        return build_tools(
            self, TavilyClient.from_config(TAVILY), groups, config.allow, config.deny,
            skill_view=bool(self.profile.skills), disposable=self._disposable,
            worker_terminal=self._task is not None,
        )

    def _blocked_tools(self) -> frozenset[str]:
        """Tools a disposable chat refuses. 'Excluded from learning' is not 'tools blocked': a task's worker keeps the
        terminal it was granted (every action still needs a manual approval); memory, self and folder edits stay blocked."""
        if self._task is not None and "terminal" in self._task.groups:
            return DISPOSABLE_BLOCKED_TOOLS - frozenset(TOOL_GROUP_TOOLS["terminal"])
        return DISPOSABLE_BLOCKED_TOOLS

    def _report_usage(self, api_usage) -> None:
        """Tell the usage hook (a task's token watchdog) what one model call used."""
        if self.usage_hook is not None and api_usage:
            self.usage_hook(api_usage.get("prompt_tokens") or 0, api_usage.get("completion_tokens") or 0)

    def open_chat(self, chat_id: Optional[str] = None) -> str:
        """Switch to a chat (a new empty one when None), rebuilding the context from its history."""
        conversations = self.sources.conversations()
        if chat_id is None:
            chat_id = conversations.create()
        elif not conversations.exists_chat(chat_id):
            raise FileNotFoundError(f"chat not found: {chat_id}")
        disposable, task = self._chat_kind(chat_id)
        if self._task is not None and task != self._task:
            raise ValueError("an agent built for a subagent task cannot open another chat")
        if disposable != self._disposable:  # another chat kind: rebuild the tools (the prompt is rebuilt below)
            self._disposable = disposable
            if task is not None:
                self._task = task
                self._become_worker(task)
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
        # whether older turns were dropped from the in-context window (safety net when compaction fails)
        self._truncated = False
        # the latest compaction summary of this chat (my notes on the part that left the context)
        self._summary: Optional[str] = None
        self._tool_counter = 0  # fallback tool call ids when the provider gives none
        self._checkpointed = False  # a partial assistant turn of the running reply is on disk (the last line)
        self._round_replies: list[str] = []  # text the model wrote before each tool round of this turn
        self._round_reasoning: list[str] = []
        # transient associative-memory hint, injected into the context of the current turn only
        self._assoc_suggestion: Optional[str] = None
        self._assoc_match_count = 0
        self._assoc_fragments: list[dict] = []  # structured hits shown to the user, see associative.RecallResult
        self._assoc_query: Optional[str] = None
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
        self.outbox: list[dict] = []  # files `send_file` queued during this reply
        self._turn_files: list[dict] = []  # every file of this reply, stored with its turn
        self._timeline = TimelineBuilder()  # order of thinking, tool steps and text of the reply being built
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
        config = load_agent(user_id, agent_id)
        provider = main_provider(config)
        sources = SourceRegistry.for_agent(config.user_id, config.id, **chat_folder_options(config))
        profile = load_profile(sources, recall_knowledge=config.recall_knowledge)
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
        if self._disposable and self._task is None:
            blocks.append(_DISPOSABLE_NOTICE)
        elif self._task is None and "append" in self._tool_handlers and self.config.has_memory:
            blocks.append(_DREAM_TASKS_NOTICE)
        if "task_start" in self._tool_handlers and (notice := delegate_notice(self.config)):
            blocks.append(notice)
        if "terminal" in self._tool_handlers and (notice := terminal_notice(self.config)):
            blocks.append(notice)
        if "agent_set_level" in self._tool_handlers:
            blocks.append(permissions_notice(self.config))
        if "agent_create" in self._tool_handlers:
            blocks.append(agents_notice(self.config))
        if self._mcp_notes:
            blocks.append("## Connected services (MCP)\n\n" + "\n\n".join(self._mcp_notes))
        return system_prompt(self.profile, blocks, block_for(self.config))

    def _refresh_mcp_tools(self) -> None:
        """Rebuild this turn's MCP tools, so connecting or disconnecting a service applies at once."""
        if self._mcp is None or not self.config.tools or not self.config.mcp:
            return
        toolset = build_mcp_tools(
            self._mcp, self.config.user_id, self.config.mcp, allow=self.config.allow, deny=self.config.deny,
            disposable=self._disposable, reserved=frozenset(TOOL_ORDER), level=self.config.level,
        )
        self._tool_specs = [*self._base_tool_specs, *toolset.specs]
        self._tool_handlers = {**self._base_tool_handlers, **toolset.handlers}
        self._mcp_tool_names = frozenset(toolset.handlers)
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

    def query_memory(
        self, query: str, top_k: int = 5, *, exclude_chat: str | None = None, only_source: str | None = None,
        only_chat: str | None = None,
    ) -> list[dict]:
        """Semantic recall over what is indexed (never syncs); `exclude_chat` leaves one chat's chunks out."""
        queried_at = perf_counter()
        results = self.index.query(
            query, top_k, exclude_chat=exclude_chat, only_source=only_source, only_chat=only_chat,
        )
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
        meta: Optional[dict] = None, channel: Optional[str] = None,
    ) -> Union[str, Iterator[tuple[str, str | dict]]]:
        """Send a user message and return the reply, updating conversation memory.

        Returns a string or a generator of (kind, payload) events. Cancellation preserves
        the accepted input and partial reply as an interrupted turn, then raises
        GenerationCancelled. A steering continuation retains the active turn even without memory.
        `channel` (`web`, `telegram`) is where the message came from; it is stored on the turn and shown to the model.
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
        self._assoc_fragments: list[dict] = []  # structured hits shown to the user, see associative.RecallResult
        self._assoc_query: Optional[str] = None
        self._assoc_job = None
        self._assoc_taken = False
        self._assoc_outcome = None
        self._assoc_parallel = False
        self._assoc_span = None
        self._model_spans = []
        self._partial = ""
        self._carried_reasoning = ""
        self._checkpointed = False
        self._round_replies = []
        self._round_reasoning = []
        self.last_actions = []
        self.outbox, self._turn_files = [], []
        self._timeline = TimelineBuilder()
        self.last_reasoning = ""
        self.last_usage = ""
        self.last_latency_report = ""
        self.last_reply = ""
        try:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
        except GenerationCancelled:
            self.messages.append({"role": "user", "content": self._stamped(user_content, ts, channel)})
            self._log_turn("user", user_content, ts=ts, **dict(meta or {}, channel=channel))
            self._remember_interrupted("", "")
            raise
        if self._associative_enabled():
            # the search runs beside the model call and sees the history without this message
            self._assoc_parallel = self._associative_parallel() and (stream or cancellation is not None)
            self._assoc_job = associative.RecallJob(
                self, list(self.messages[1:]), user_input, chat_id=self.chat_id,
            ).start()
        self.messages.append({"role": "user", "content": self._stamped(user_content, ts, channel)})
        self._log_turn("user", user_content, ts=ts, **dict(meta or {}, channel=channel))  # `meta`: source/task_ids of a wake turn
        if not continuation:
            self._guard_context()
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
    def _stamped(content: str | list[dict], ts: str, channel: Optional[str] = None) -> str | list[dict]:
        """Prefix a user turn with its local timestamp (and channel, when known) so the model knows when and where."""
        stamp = datetime.fromisoformat(ts).isoformat(sep=" ", timespec="minutes")
        prefix = f"[{stamp} via {_CHANNEL_NAMES.get(channel, channel)}]" if channel else f"[{stamp}]"
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
                        self._timeline.text(chunk)
                    elif kind == "reasoning" and isinstance(chunk, str):
                        reasoning += chunk
                        self._timeline.reasoning(chunk)
                    elif kind == "tool":
                        self._timeline.tool()
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
        max_hops = self.config.max_tool_hops or AGENT_MAX_TOOL_HOPS
        for hop in range(max_hops + 1):
            cancellation.raise_if_cancelled()
            is_last = hop == max_hops
            tools = None if is_last else (self._tool_specs or None)
            while True:  # runs again when a memory hit pauses the call
                yield from self._take_assoc_result()
                context = self._context()
                if is_last:
                    context = context + [{"role": "system", "content": _FINAL_NUDGE}]
                elif warning := _hops_warning(hop, max_hops):
                    context = context + [{"role": "system", "content": warning}]
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
                            # A chunk the provider already produced is never thrown away because a memory hit
                            # cancelled the call meanwhile: providers that emit reasoning and the finished tool
                            # calls only at the end of the call (Claude subscription) would otherwise lose both.
                            # The user's own interrupt still stops at once.
                            cancellation.raise_if_cancelled()
                            if not (isinstance(chunk, dict) or (isinstance(chunk, tuple) and chunk[0] == "reasoning")):
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
                                # an already cancelled call is not broken off here: its remaining chunks (the finished
                                # tool calls) are still read, and the provider raises when there is nothing left
                                if job is not None and cut_ok.is_set() and job.has_hit() and not call_token.is_cancelled():
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
                self._report_usage(api_usage)
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
                yield from self._iter_tools(tool_calls, cancellation)
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
        result = job.result()
        suggestion, count = result
        if not job.failed:
            self._assoc_query = getattr(result, "query", None)
            self._assoc_fragments = list(getattr(result, "fragments", None) or []) if suggestion else []
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
        event = {"state": "done", "matches": self._assoc_match_count}
        if self._assoc_fragments:
            event["fragments"] = self._assoc_fragments
        if self._assoc_query:
            event["query"] = self._assoc_query
        return event

    def _close_assoc_job(self) -> None:
        """Stop the recall job if it is still running and remember how long it ran."""
        job = self._assoc_job
        if job is None:
            return
        self._assoc_job = None
        if not self._assoc_taken:
            if job.ready() and not job.failed and not job.result()[0]:
                self._assoc_outcome = "none"
                self._assoc_query = getattr(job.result(), "query", None)
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
        max_hops = self.config.max_tool_hops or AGENT_MAX_TOOL_HOPS
        for hop in range(max_hops + 1):
            is_last = hop == max_hops
            tools = None if is_last else (self._tool_specs or None)
            context = self._context()
            if is_last:
                context = context + [{"role": "system", "content": _FINAL_NUDGE}]
            elif warning := _hops_warning(hop, max_hops):
                context = context + [{"role": "system", "content": warning}]
            model_started_at = perf_counter()
            result = self.provider.invoke(
                context, self.config.models.chat,
                stream=False, reasoning=self.config.reasoning,
                reasoning_tokens=self.config.reasoning_tokens,
                reasoning_effort=self.config.reasoning_effort, tools=tools,
                zdr=self.zdr_required,
            )
            self._model_spans.append((model_started_at, perf_counter()))
            self._report_usage(result.get("usage"))
            reasoning = result.get("reasoning") or ""
            reply = result.get("response") or ""
            tool_calls = result.get("tool_calls")
            if tool_calls and not is_last:
                tool_calls = _dedupe_tool_calls(tool_calls)
                self._remember_tool_calls(reply, reasoning, tool_calls)
                self._timeline.reasoning(reasoning)
                self._timeline.text(reply)
                for _ in self._run_tools(tool_calls):
                    self._timeline.tool()
                continue
            self.last_reasoning = reasoning
            self._timeline.reasoning(reasoning)
            self._timeline.text(reply)
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
        self._round_replies.append(reply)
        self._round_reasoning.append(reasoning)
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
        return [payload for kind, payload in self._iter_tools(tool_calls, cancellation) if kind == "tool"]

    def _iter_tools(
        self, tool_calls: list[dict], cancellation: CancellationToken | None = None,
    ) -> Iterator[tuple[str, dict]]:
        """Run the tools one by one, yielding `("tool_start", {id, name, arguments})` right before each runs and
        `("tool", action + id)` when it is done (the id is the provider's call id, else a per-turn counter)."""
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
            self._tool_counter += 1
            call_id = str(tc.get("id") or f"call-{self._tool_counter}")
            yield "tool_start", {"id": call_id, "name": name, "arguments": args}
            handler = self._tool_handlers.get(name)
            failed = False
            if self._disposable and (name in self._blocked_tools() or name in self._mcp_tool_names):  # defence in depth: the specs already omit these
                result, failed = f"error: '{name}' is not available in a disposable chat", True
            elif handler is not None and not tool_allowed(self.config.level, name, self._mcp_tool_names):
                result, failed = level_refusal(self.config.level, name, self._mcp_tool_names), True  # the level is a ceiling, even if a handler exists
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
            files, self.outbox = self.outbox, []
            self._turn_files.extend(files)
            yield "tool", {**actions[-1], "id": call_id, **({"files": files} if files else {})}
        self._checkpoint()
        if cancellation is not None:
            cancellation.raise_if_cancelled()

    def _checkpoint(self) -> None:
        """Save what this reply has done so far (text, reasoning, tool actions) after each tool round.

        A restart in the middle of a long reply (a release that restarts the server, a crash) would otherwise lose
        everything: the turn is only written when the reply ends. The checkpoint is replaced by the next one and by
        the final turn; if it stays the last line, the chat shows the reply as cut off and the model sees it.
        """
        if not self.config.has_memory or self._disposable:
            return
        text = "\n\n".join(r.strip() for r in self._round_replies if r and r.strip())
        reasoning = "\n\n".join(r.strip() for r in self._round_reasoning if r and r.strip())
        try:
            self.sources.conversations().checkpoint(
                self.chat_id, text,
                replace=self._checkpointed,
                model=self.config.models.chat,
                reasoning=reasoning or None,
                actions=self.last_actions or None,
                media=self._turn_files or None,
                timeline=self._timeline.build(len(self.last_actions)),
            )
            self._checkpointed = True
        except Exception:  # a failed checkpoint must never break the reply
            logger.warning("could not checkpoint the running reply of chat %s", self.chat_id, exc_info=True)

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
            "media": self._turn_files or None,
            "timeline": self._timeline.build(len(self.last_actions)),
            "associative_matches": self._assoc_match_count if self._assoc_outcome in ("found", "none") else None,
            "associative_skipped": True if self._assoc_outcome == "skipped" else None,
            "associative_fragments": (self._assoc_fragments or None) if self._assoc_outcome == "found" else None,
            "associative_query": self._assoc_query if self._assoc_outcome in ("found", "none") else None,
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
    def _reply_content(reply: str | list[dict], interrupted: bool, checkpoint: bool = False) -> str | list[dict]:
        if not interrupted:
            return reply
        marker = _CHECKPOINT_MARKER if checkpoint else _INTERRUPTED_MARKER
        if isinstance(reply, list):
            return [*reply, {"type": "text", "text": marker}]
        return f"{reply}\n\n{marker}"

    def _log_turn(self, role: str, content: str | list[dict], **extra) -> None:
        """Persist a turn to the active chat's jsonl (independent of in-context memory)."""
        extra = {k: v for k, v in extra.items() if v is not None}
        replaces = role == "assistant" and self._checkpointed  # the final turn takes the place of the checkpoint
        if replaces:
            self._checkpointed = False
        self.sources.conversations().append(self.chat_id, role, content, drop_checkpoint=replaces, **extra)

    # ------------------------------------------------------------------ context window

    def _load_recent_context(self) -> None:
        """Seed the context with the active chat: the latest compaction summary and the turns after it."""
        if not self.config.has_memory:
            return
        history = self.sources.conversations().load(self.chat_id)
        found = compaction.last_compaction(history)
        if found is not None:
            self._summary = found[1]["content"]
            history = history[found[1]["covers_until"]:]
        for position, t in enumerate(history):
            if t.get("role") not in ("user", "assistant"):
                continue
            content = self._reply_content(t["content"], bool(t.get("interrupted")), bool(t.get("checkpoint")))
            if t["role"] == "user" and t.get("ts"):
                content = self._stamped(content, t["ts"], t.get("channel"))
            if t["role"] == "assistant" and t.get("actions"):
                restored = self._restore_tool_rounds(t, position)
                if restored is not None:
                    self.messages.extend(restored)
                    continue
            self.messages.append({"role": t["role"], "content": content})
        # seed the context input size from the most recent persisted usage (or the latest compaction)
        for t in reversed(history):
            if t.get("role") == compaction.COMPACTION_ROLE:
                if isinstance(t.get("tokens_after"), int):
                    self._ctx_input_tokens = t["tokens_after"]
                break
            u = t.get("usage")
            if u and u.get("in") is not None:
                if t.get("model", self._default_chat_model) == self.config.models.chat:
                    self._ctx_input_tokens = u["in"]
                break

    def _restore_tool_rounds(self, turn: dict, position: int) -> Optional[list[dict]]:
        """Rebuild the tool rounds of a stored assistant turn as context messages (calls, results, final text).

        The chat file keeps each action (name, arguments, result cut to a few hundred characters) and the timeline
        that orders text and tools, so a reloaded chat keeps what the model had done instead of only what it said.
        Call ids are not stored: they are generated here. None when the stored data does not add up (the caller
        then falls back to the text of the turn alone).
        """
        actions = turn.get("actions")
        if not isinstance(actions, list) or not all(isinstance(a, dict) and a.get("name") for a in actions):
            return None
        segments = turn.get("timeline")
        if not segments:
            segments = [{"type": "tool", "index": i} for i in range(len(actions))]
        rounds: list[tuple[str, list[int]]] = []  # (text said before the calls, indexes of the calls of that round)
        text, calls, seen = "", [], []
        for segment in segments:
            kind = segment.get("type") if isinstance(segment, dict) else None
            if kind == "tool":
                index = segment.get("index")
                if not isinstance(index, int) or not 0 <= index < len(actions):
                    return None
                calls.append(index)
                seen.append(index)
            elif kind == "text":
                if calls:  # text after tools closes that round
                    rounds.append((text, calls))
                    text, calls = "", []
                text = (text + "\n\n" if text else "") + str(segment.get("text") or "")
        if calls:
            rounds.append((text, calls))
            text = ""
        if sorted(seen) != list(range(len(actions))):
            return None
        trailing = text
        interrupted = bool(turn.get("interrupted"))
        messages: list[dict] = []
        for number, (text, indexes) in enumerate(rounds):
            calls, results = [], []
            for index in indexes:
                action = actions[index]
                call_id = f"restored-{position}-{index}"
                arguments = action.get("arguments")
                calls.append({
                    "id": call_id, "type": "function",
                    "function": {
                        "name": action["name"],
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments or {}),
                    },
                })
                result = str(action.get("result") or "")
                if result.endswith("…"):
                    result += " [stored result, cut for length]"
                results.append({"role": "tool", "tool_call_id": call_id, "name": action["name"], "content": result})
            messages.append({"role": "assistant", "content": text, "tool_calls": calls})
            messages.extend(results)
        if interrupted:
            marker = self._reply_content(trailing, True, bool(turn.get("checkpoint")))
            messages.append({"role": "assistant", "content": marker.lstrip()})
        elif turn.get("content"):
            messages.append({"role": "assistant", "content": turn["content"]})
        return messages

    # ------------------------------------------------------------------ compaction

    def _context_tokens(self) -> int:
        """Tokens the context weighs now: the last real input plus what came after it, else a chars/4 estimate."""
        if self._ctx_input_tokens is not None:
            last = self.messages[-1] if len(self.messages) > 1 else None
            tail = compaction.message_chars(last) // 4 if last and last.get("role") != "tool" else 0
            return self._ctx_input_tokens + tail
        chars = sum(compaction.message_chars(m) for m in self._context())
        chars += len(json.dumps(self._tool_specs)) if self._tool_specs else 0
        return chars // 4

    def _compaction_model(self) -> tuple[LLMProvider, str, bool]:
        """The summary model: `compaction.model`, else the chat's own; ZDR applies only to OpenRouter models."""
        model = self.config.compaction.model
        if model is None or model == self.config.models.chat:
            return self.provider, self.config.models.chat, self.zdr_required
        zdr = self.zdr_required and not is_claude_subscription(model)
        return self.provider_named(provider_of(model, self.config.provider_name), "compaction.model"), model, zdr

    def compaction_due(self) -> bool:
        """Whether the context passed this agent's compaction trigger."""
        if not self.config.has_memory or not self.config.compaction.enabled or len(self.messages) < 3:
            return False
        self._ensure_max_context()
        trigger, _ = compaction.thresholds(self.config.compaction, self._max_context or 0)
        return trigger is not None and self._context_tokens() >= trigger

    def compact(self, *, manual: bool = False, instructions: str = "") -> Optional[compaction.CompactionResult]:
        """Summarise the older part of the context with the summary model; None when there is nothing to do.

        The system prompt and a recent tail stay as they are; the summary (merged with the previous one) replaces
        the rest and a `compaction` turn records it in the chat. Raises when the summary model fails.
        """
        if not self.config.has_memory or (not manual and not self.compaction_due()):
            return None
        self._ensure_max_context()
        _, keep = compaction.thresholds(self.config.compaction, self._max_context or 0)
        total = self._context_tokens()
        extra = len(compaction.summary_message(self._summary)["content"]) if self._summary else 0
        extra += len(json.dumps(self._tool_specs)) if self._tool_specs else 0
        cut = compaction.split(self.messages, total, keep, force=manual, extra_chars=extra)
        small = False
        if cut is None and manual:
            # a short chat fits entirely in the normal tail: keep only its latest 20 % and summarise the rest
            cut = compaction.split(self.messages, total, total * compaction.SMALL_CHAT_KEEP_PERCENT // 100, force=True, extra_chars=extra)
            small = cut is not None
        if cut is None:
            return None
        older, tail = self.messages[1:cut.start], self.messages[cut.start:]
        provider, model, zdr = self._compaction_model()
        text = compaction.transcript(older, self._summary, instructions)
        try:
            summary = compaction.summarise(provider, model, text, zdr=zdr)
        except Exception:
            logger.warning("compaction summary failed once for chat %s; retrying", self.chat_id, exc_info=True)
            summary = compaction.summarise(provider, model, text, zdr=zdr)
        previous = len(compaction.summary_message(self._summary)["content"]) // 4 if self._summary else 0
        after = max(0, total - cut.summarised_tokens - previous) + len(compaction.summary_message(summary)["content"]) // 4
        result = compaction.CompactionResult(summary, total, after, model, manual, small=small)
        users_in_tail = sum(1 for m in tail if m.get("role") == "user")
        history = self.sources.conversations().load(self.chat_id)
        covers = compaction.covers_until(history, users_in_tail)
        self.messages = [self.messages[0], *tail]
        self._summary = summary
        self._truncated = False
        self._ctx_input_tokens = after
        self._prev_breakdown = None
        self._log_turn(compaction.COMPACTION_ROLE, summary, **compaction.record(result, covers))
        logger.info(
            "compacted chat %s/%s/%s: %d -> %d tokens with %s%s", self.config.user_id, self.config.id,
            self.chat_id, total, after, model, " (manual)" if manual else "",
        )
        return result

    def _guard_context(self) -> None:
        """Before a model call: compact when due, and cut the oldest messages when the context nears the window.

        The cut is the safety net for a summary model that fails: the chat keeps working, with a cutoff marker.
        """
        if not self.config.has_memory:
            return
        if self.compaction_due():
            try:
                self.compact()
                return
            except Exception:
                logger.exception("compaction failed for chat %s", self.chat_id)
        self._ensure_max_context()
        window = self._max_context or 0
        if not window or self._context_tokens() < window * compaction.HARD_LIMIT_PERCENT // 100:
            return
        _, keep = compaction.thresholds(self.config.compaction, window)
        cut = compaction.split(self.messages, self._context_tokens(), keep or window // 5, force=True)
        if cut is not None:
            self.messages = [self.messages[0], *self.messages[cut.start:]]
            self._truncated = True
            self._ctx_input_tokens = None
            logger.warning("context of chat %s cut at the hard limit (compaction unavailable)", self.chat_id)

    def _context(self) -> list[dict]:
        """The messages actually sent to the model: the compaction summary and a cutoff marker when they apply."""
        ctx = self.messages
        head = [compaction.summary_message(self._summary)] if self._summary else []
        if self._truncated:
            head.append({"role": "system", "content": "[CUTOFF CONVERSATION]"})
        if head:
            ctx = [self.messages[0], *head, *self.messages[1:]]
        if self._partial:
            ctx = ctx + [{"role": "assistant", "content": self._partial.rstrip()}]
        if self._assoc_suggestion:
            if self._partial:  # the continuation after a pause: the hint closes the context, with the cut note
                return ctx + [{"role": "system", "content": f"{self._assoc_suggestion}\n\n{_HIT_CUT_NOTE}"}]
            # once, right after this turn's user message: later tool rounds do not repeat it
            at = next((i for i in range(len(ctx) - 1, -1, -1) if ctx[i].get("role") == "user"), None)
            hint = {"role": "system", "content": self._assoc_suggestion}
            ctx = ctx + [hint] if at is None else ctx[: at + 1] + [hint] + ctx[at + 1 :]
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
