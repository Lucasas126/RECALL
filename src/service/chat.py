from __future__ import annotations

import base64
import binascii
import os
import re
import threading
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from queue import SimpleQueue
from time import monotonic
from typing import Callable, Iterator, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.agent.agent import Agent, embeddings_provider
from src.agent.tools import chat_folder_options
from src.agent.dream import DEFAULT_DREAM_PROMPT
from src.config.agents import AgentStore
from src.config.favorites import ModelFavorites
from src.config.loader import load_agent, load_agent_config, load_user, user_models
from src.config.models import (
    AgentConfig, DreamConfig, FolderConfig, MemoryLimits, ReasoningEffort, ResolvedAgent, UserConfig,
)
from src.agent.tool_groups import DEFAULT_TOOL_GROUPS, TOOL_GROUP_CATALOG
from src.config.preferences import UserPreferences, UserPreferencesStore
from src.config.settings import (
    CHAT_NAMING, CONFIG_DIR, DATA_DIR, MCP, PDF, SPEECH_TO_TEXT, TAVILY, TELEGRAM_ALLOWED_USERS_ENV,
    VIDEO_DECOMPOSITION, WEB_PUBLIC_URL,
)
from src.core.index import VectorIndex
from src.core.sources.agent_files import SOUL_MAX_CHARS, AgentFilesSource, MemoryTarget
from src.core.sources.conversations import ConversationsSource
from src.core.sources.registry import SourceRegistry
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.catalog import CatalogUnavailable, list_openrouter_models
from src.providers.catalog import list_openrouter_transcription_models
from src.providers.mcp import McpManager
from src.providers.openrouter import OpenRouterProvider
from src.providers.provider import get_provider
from src.providers.tavily import TavilyClient
from src.providers.telegram_notify import TelegramNotifier
from src.service.dream import DreamDependencies, DreamManager, format_local_time, parse_dream_command
from src.utils.logger import get_logger
from src.utils.media import VideoDecomposition, decompose_video
from src.utils.pdf import extract_pdf_text, run_tesseract
from src.utils.i18n import InputError, UserMessageError, message
from src.web.auth import verify_password

logger = get_logger("service")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_REASONING_SUPPORT_TTL_SECONDS = 600
_NAMING_PROMPT = (
    "Name the conversation below with a short title of at most six words, in the language used "
    "in the conversation. Reply with the title only, without quotes or punctuation at the end."
)
_NAMING_TURN_CHARS = 1000

AgentFactory = Callable[[str, str, Optional[str]], Agent]
IndexFactory = Callable[[ResolvedAgent, SourceRegistry], VectorIndex]


_DECOMPOSITION_NOTICE = {
    "en": (
        "This model supports images but not video. {videos} video(s) were decomposed into "
        "{frames} frame(s) and their audio. The model only sees these frames, so motion or "
        "events between them may be missed."
    ),
    "es": (
        "Este modelo admite imágenes, pero no vídeo. {videos} vídeo(s) se han descompuesto en "
        "{frames} fotograma(s) y su audio. El modelo solo ve estos fotogramas, así que puede "
        "perder movimiento o eventos entre ellos."
    ),
}
_DECOMPOSITION_TRUNCATED = {
    "en": " Only the first part of at least one video was sampled.",
    "es": " Solo se ha muestreado la primera parte de al menos un vídeo.",
}


def _decomposition_notice(language: str, decompositions: list[VideoDecomposition]) -> str | None:
    """Return the localized warning shown when videos were replaced by frames, or None."""
    if not decompositions:
        return None
    code = language.split("-", 1)[0].lower()
    code = code if code in _DECOMPOSITION_NOTICE else "en"
    notice = _DECOMPOSITION_NOTICE[code].format(
        videos=len(decompositions),
        frames=sum(len(item.frames) for item in decompositions),
    )
    if any(item.truncated for item in decompositions):
        notice += _DECOMPOSITION_TRUNCATED[code]
    return notice


def _naming_transcript(turns: list[dict]) -> str:
    lines = []
    for turn in turns:
        content = turn.get("content", "")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if part.get("type") == "text")
        text = " ".join(str(content).split())[:_NAMING_TURN_CHARS]
        lines.append(f"{turn['role']}: {text}")
    return "\n".join(lines)


class ServiceError(UserMessageError):
    """Base error of the chat service; channels translate subclasses to their own responses."""


class UnknownUser(ServiceError):
    pass


class UnknownAgent(ServiceError):
    pass


class UnknownChat(ServiceError):
    pass


class AgentBusy(ServiceError):
    pass


class UnknownDreamSession(ServiceError):
    """A dream session or SOUL version that does not exist."""


class ChatAttachment(BaseModel):
    """A validated data URL attached to a user message."""

    model_config = ConfigDict(extra="forbid")

    modality: Literal["image", "video", "audio", "document"]
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=128)
    format: str = Field(min_length=1, max_length=16)
    data_url: str = Field(min_length=32, max_length=30_000_000)


@dataclass(frozen=True)
class ChatEvent:
    """One piece of an agent reply, independent of the channel that delivers it.

    `kind` is `reasoning` (thinking tokens), `text` (answer tokens), `tool` (an action the agent
    took, in `data`: name, arguments, result, error), `usage` (context report, emitted once
    after the reply), `interrupted` (partial answer in `text`, reasoning/actions in `data`) or
    `notice` (a warning about how the request was handled, in `text`, sent before the reply).
    Non-streaming replies arrive as `tool` events followed by one `text` event. All events
    carry a generation id so adapters can distinguish overlapping deliveries.
    """

    kind: Literal["reasoning", "text", "tool", "usage", "interrupted", "memory", "notice"]
    text: str = ""
    data: Optional[dict] = None
    generation_id: str = ""


@dataclass
class _MessageRun:
    chat_id: str
    text: str
    attachments: list[dict]
    stream: Optional[bool]
    continuation: bool = False
    generation_id: str = field(default_factory=lambda: uuid4().hex)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    events: SimpleQueue[ChatEvent | Exception | None] = field(default_factory=SimpleQueue)


class _AgentSlot:
    """A lazily built agent, its worker queue and locks for execution and scheduling."""

    def __init__(self) -> None:
        self.agents: dict[str, Agent] = {}
        self.lock = threading.Lock()
        self.state = threading.Lock()
        self.pending: dict[str, deque[_MessageRun]] = {}
        self.active: dict[str, _MessageRun] = {}
        self.workers: dict[str, threading.Thread] = {}

    @property
    def worker(self) -> threading.Thread | None:
        """Return one worker for lifecycle inspection."""
        return next(iter(self.workers.values()), None)


class ChatService:
    """Channel-agnostic chat operations for a user: agents, chats and sending messages.

    Every call is scoped to a `user_id` that the channel has already resolved from its own
    identity (web login, telegram id, ...). Returns plain dicts and events, never internal objects.
    """

    def __init__(
        self, agent_factory: AgentFactory | None = None, mcp: McpManager | None = None,
        dream: DreamManager | None = None, index_factory: IndexFactory | None = None,
    ):
        self._index_factory = index_factory or self._build_index
        self._mcp = mcp or McpManager(MCP, CONFIG_DIR)
        self._agent_factory = agent_factory or (
            lambda user_id, agent_id, chat_id: Agent.from_config(user_id, agent_id, chat_id, self._mcp)
        )
        self._slots: dict[tuple[str, str], _AgentSlot] = {}
        self._slots_guard = threading.Lock()
        self._favorites = ModelFavorites(CONFIG_DIR)
        self._preferences = UserPreferencesStore(CONFIG_DIR)
        self._agents = AgentStore(CONFIG_DIR, DATA_DIR)
        # (model id -> (monotonic time, supported)) so opening chats does not refetch the catalog
        self._reasoning_support: dict[str, tuple[float, bool]] = {}
        self._dream = dream or DreamManager(self._dream_dependencies())

    @staticmethod
    def _build_index(config: ResolvedAgent, sources: SourceRegistry) -> VectorIndex:
        """The agent's vector index with its configured embeddings provider and model."""
        chat = get_provider(config.provider_name, api_key=config.api_key)
        return VectorIndex.for_agent(
            config.user_id, config.id, sources, embeddings_provider(config, chat), config.models.embeddings,
        )

    def _dream_dependencies(self) -> DreamDependencies:
        """Production wiring of dream sessions: real agents, providers, index, web access and Telegram."""
        notifier = TelegramNotifier.from_env(TELEGRAM_ALLOWED_USERS_ENV)

        return DreamDependencies(
            resolve_agent=load_agent,
            provider=lambda config: get_provider(config.provider_name, api_key=config.api_key),
            index=lambda config, sources: self._index_factory(config, sources),
            web_client=lambda: TavilyClient.from_config(TAVILY),
            agents=self._all_agents,
            language=self.language,
            notifier=notifier.send if notifier is not None else None,
            public_url=WEB_PUBLIC_URL or None,
        )

    def _all_agents(self) -> list[tuple[str, str]]:
        """Every (user, agent) pair configured under the config folder, for the dream scheduler."""
        pairs: list[tuple[str, str]] = []
        if not CONFIG_DIR.is_dir():
            return pairs
        for user_dir in sorted(CONFIG_DIR.iterdir()):
            if not (user_dir / "user.yaml").is_file() or not _ID_RE.match(user_dir.name):
                continue
            try:
                pairs.extend((user_dir.name, agent_id) for agent_id in load_user(user_dir.name).agents)
            except Exception:  # one broken user file must not stop the others' schedules
                logger.exception("dream scheduler could not read user %s", user_dir.name)
        return pairs

    # ------------------------------------------------------------------ users & agents

    def login(self, user_id: str) -> dict:
        """Resolve an existing user and return their profile and agents; raises UnknownUser."""
        user = self._user(user_id)
        return {
            "id": user_id,
            "name": user.name,
            "language": user.language,
            "provider": user.provider.name,
            "agents": self.list_agents(user_id),
        }

    def authenticate(self, user_id: str, password: str) -> dict:
        """Verify configured credentials and return the user's profile."""
        try:
            user = self._user(user_id)
        except UnknownUser:
            raise UnknownUser("Invalid user ID or password") from None
        if not verify_password(password, user.password_hash):
            raise UnknownUser("Invalid user ID or password")
        return self.login(user_id)

    def list_agents(self, user_id: str) -> list[dict]:
        user = self._user(user_id)
        agents = []
        for agent_id in user.agents:
            cfg = load_agent_config(user_id, agent_id)
            agents.append({"id": cfg.id, "name": cfg.name})
        return agents

    def list_agent_configs(self, user_id: str) -> list[dict]:
        """Full editable configuration of every agent the user owns."""
        user = self._user(user_id)
        return [self._agent_view(user_id, self._agent_config(user_id, agent_id)) for agent_id in user.agents]

    def list_tool_groups(self, user_id: str) -> list[dict]:
        """Return the tool-group catalog; `default` marks the groups an agent without `tool_groups` uses."""
        self._user(user_id)
        return [{**item, "default": item["id"] in DEFAULT_TOOL_GROUPS} for item in TOOL_GROUP_CATALOG]

    # ------------------------------------------------------------------ mcp connections

    def list_mcp_servers(self, user_id: str) -> list[dict]:
        """Return the shared MCP catalog with this user's connection status for each server."""
        self._user(user_id)
        return [self._mcp.status(user_id, server_id) for server_id in self._mcp.servers]

    def list_mcp_tools(self, user_id: str, server_id: str) -> list[dict]:
        """List a connected server's tools for this user, to choose them per agent."""
        self._user(user_id)
        return [
            {"name": tool.name, "description": tool.description, "read_only": tool.read_only}
            for tool in self._mcp.list_tools(user_id, server_id)
        ]

    def connect_mcp(self, user_id: str, server_id: str) -> dict:
        """Start the server's sign-in for this user; returns the status with the sign-in URL."""
        self._user(user_id)
        return self._mcp.start_auth(user_id, server_id)

    def disconnect_mcp(self, user_id: str, server_id: str) -> dict:
        """Stop this user's server and delete the user's stored credentials for it."""
        self._user(user_id)
        return self._mcp.disconnect(user_id, server_id)

    def close(self) -> None:
        """Stop background resources such as MCP server processes and the dream scheduler."""
        self._dream.stop()
        self._mcp.close()

    def start_dream_scheduler(self, interval_seconds: float = 60.0) -> None:
        """Run scheduled dream sessions from this process (the web and Telegram processes both may)."""
        self._dream.start_scheduler(interval_seconds)

    def _check_mcp(self, config: AgentConfig) -> None:
        for server_id in config.chat_config.mcp:
            self._mcp.server(server_id)

    def get_agent(self, user_id: str, agent_id: str) -> dict:
        self._owned_agent(user_id, agent_id)
        return self._agent_view(user_id, self._agent_config(user_id, agent_id))

    def _agent_view(self, user_id: str, config: AgentConfig) -> dict:
        """An agent's config as the API shows it (the personality is SOUL.md, served by the soul routes)."""
        return config.model_dump(mode="json")

    def create_agent(self, user_id: str, config: AgentConfig) -> dict:
        """Create an agent; its personality is written afterwards to SOUL.md through `update_soul`."""
        self._user(user_id)
        self._check_mcp(config)
        self._check_folders(config.folders)
        created = self._agents.create(user_id, config)
        return self._agent_view(user_id, created)

    def update_agent(self, user_id: str, agent_id: str, config: AgentConfig) -> dict:
        """Save an idle agent's configuration; a changed id also moves its data folder and preference."""
        self._owned_agent(user_id, agent_id)
        self._check_mcp(config)
        self._check_folders(config.folders)
        slot = self._slot(user_id, agent_id)
        with slot.state:
            if slot.active or slot.pending:
                raise AgentBusy("wait for this agent's active and queued replies before editing it")
            with slot.lock:
                updated = self._agents.update(user_id, agent_id, config)
                slot.agents.clear()
        if updated.id != agent_id:
            self._forget_slot(user_id, agent_id)
            self._replace_preferred_agent(user_id, agent_id, updated.id)
        return self._agent_view(user_id, updated)

    def delete_agent(self, user_id: str, agent_id: str) -> None:
        """Remove an idle agent's configuration; its data folder is kept on disk."""
        self._owned_agent(user_id, agent_id)
        slot = self._slot(user_id, agent_id)
        with slot.state:
            if slot.active or slot.pending:
                raise AgentBusy("wait for this agent's active and queued replies before deleting it")
            with slot.lock:
                self._agents.delete(user_id, agent_id)
                slot.agents.clear()
        self._forget_slot(user_id, agent_id)
        self._replace_preferred_agent(user_id, agent_id, None)

    def _agent_config(self, user_id: str, agent_id: str) -> AgentConfig:
        try:
            return load_agent_config(user_id, agent_id)
        except FileNotFoundError as exc:
            raise UnknownAgent("unknown agent: {agent_id}", agent_id=agent_id) from exc

    def _replace_preferred_agent(self, user_id: str, old_id: str, new_id: str | None) -> None:
        if self._preferences.get(user_id).agent_id == old_id:
            self._preferences.update(user_id, UserPreferences(agent_id=new_id))

    def _forget_slot(self, user_id: str, agent_id: str) -> None:
        with self._slots_guard:
            self._slots.pop((user_id, agent_id), None)

    def get_preferences(self, user_id: str) -> dict:
        """Read current user-wide preferences, including configuration-derived defaults."""
        user = self._user(user_id)
        return self._resolve_preferences(user, self._preferences.get(user_id)).model_dump()

    def language(self, user_id: str) -> Literal["en", "es"]:
        """Read the current alert language without changing cached agents or chat state."""
        user = self._user(user_id)
        return self._preferences.get(user_id).language_for(user.language)

    def update_preferences(self, user_id: str, changes: UserPreferences) -> dict:
        """Persist explicit changes without modifying any open chat or running agent."""
        user = self._user(user_id)
        if changes.default_llm_model is not None:
            models = self.list_models(user_id, changes.default_llm_model, zdr=True)
            if not any(model["id"] == changes.default_llm_model for model in models):
                raise InputError("model is not available in the OpenRouter chat catalog with the selected ZDR filter")
        if changes.default_stt_model is not None:
            models = self.list_transcription_models(user_id, changes.default_stt_model, zdr=SPEECH_TO_TEXT.zdr)
            if not any(model["id"] == changes.default_stt_model for model in models):
                raise InputError("The speech-to-text model is not available with the required routing policy.")
        saved = self._preferences.update(
            user_id, changes, validate=lambda preferences: self._resolve_preferences(user, preferences),
        )
        return self._resolve_preferences(user, saved).model_dump()

    def _resolve_preferences(self, user: UserConfig, saved: UserPreferences) -> UserPreferences:
        resolved = UserPreferences.model_validate({
            "language": saved.language_for(user.language),
        } | saved.model_dump(exclude_unset=True))
        agent_id = resolved.agent_id
        if agent_id is None:
            agent_id = user.agents[0] if user.agents else None
        if agent_id is not None:
            self._check_id("agent", agent_id, UnknownAgent)
            if agent_id not in user.agents:
                raise UnknownAgent(
                    "agent '{agent_id}' is not owned by user '{user_id}'", agent_id=agent_id, user_id=user.id,
                )
            try:
                load_agent_config(user.id, agent_id)
            except FileNotFoundError as exc:
                raise UnknownAgent("unknown agent: {agent_id}", agent_id=agent_id) from exc
        return resolved.model_copy(update={
            "agent_id": agent_id,
            "default_llm_model": resolved.default_llm_model or user.models.chat,
            "default_stt_model": resolved.default_stt_model or user.models.stt,
        })

    def list_models(self, user_id: str, query: str = "", *, zdr: bool = True) -> list[dict]:
        """List searchable chat models for an OpenRouter user, requiring ZDR by default."""
        user = self._user(user_id)
        if user.provider.name != "openrouter":
            raise InputError("model browsing is only available for the OpenRouter chat provider")
        return [model.model_dump() for model in list_openrouter_models(query, zdr=zdr)]

    def list_transcription_models(self, user_id: str, query: str = "", *, zdr: bool = True) -> list[dict]:
        """List OpenRouter speech-to-text models for the current user."""
        user = self._user(user_id)
        if user.provider.name != "openrouter":
            raise InputError("speech-to-text model browsing requires the OpenRouter chat provider")
        return [
            model.model_dump()
            for model in list_openrouter_transcription_models(query, zdr=zdr)
        ]

    def list_favorite_models(self, user_id: str) -> list[str]:
        """Return saved OpenRouter IDs, including favorites no longer in the ZDR catalog."""
        self._user(user_id)
        return self._favorites.list(user_id)

    def add_favorite_model(self, user_id: str, model_id: str, *, zdr: bool = True) -> list[str]:
        """Save a model after checking availability under the explicit catalog filter."""
        models = self.list_models(user_id, model_id, zdr=zdr)
        if not any(model["id"] == model_id for model in models):
            raise InputError("model is not available in the OpenRouter chat catalog with the selected ZDR filter")
        return self._favorites.update(user_id, model_id, favorite=True)

    def remove_favorite_model(self, user_id: str, model_id: str) -> list[str]:
        self._user(user_id)
        return self._favorites.update(user_id, model_id, favorite=False)

    # ------------------------------------------------------------------ chats

    def list_chats(self, user_id: str, agent_id: str, keep: Optional[str] = None) -> list[dict]:
        """Chat summaries, most recently active first; unused empty chats (except `keep`) are removed."""
        conversations = self._conversations(user_id, agent_id)
        slot = self._slot(user_id, agent_id)
        with slot.state:
            conversations.prune_empty(self._kept_chats(slot, keep))
        summaries = [conversations.summary(chat_id) for chat_id in conversations.chats()]
        return sorted(summaries, key=lambda s: s["updated"], reverse=True)

    def list_all_chats(self, user_id: str, keep: Optional[str] = None) -> list[dict]:
        """Chat summaries across all of the user's agents, each labelled with its agent."""
        chats = []
        for agent in self.list_agents(user_id):
            for chat in self.list_chats(user_id, agent["id"], keep):
                chats.append({**chat, "agent_id": agent["id"], "agent_name": agent["name"]})
        return sorted(chats, key=lambda chat: chat["updated"], reverse=True)

    def create_chat(self, user_id: str, agent_id: str) -> dict:
        """Create a chat, discarding any other unused empty ones so they never pile up."""
        conversations = self._conversations(user_id, agent_id)
        slot = self._slot(user_id, agent_id)
        with slot.state:
            conversations.prune_empty(self._kept_chats(slot))
            return conversations.summary(conversations.create())

    def get_chat(self, user_id: str, agent_id: str, chat_id: str) -> dict:
        """A chat's summary and its user/assistant messages."""
        conversations = self._conversations(user_id, agent_id)
        turns = self._load(conversations, chat_id)
        messages = [
            {
                k: t[k] for k in (
                    "role", "content", "ts", "reasoning", "actions", "interrupted",
                    "associative_matches", "associative_skipped", "usage", "usage_report",
                )
                if t.get(k) is not None
            }
            for t in turns
            if t.get("role") in ("user", "assistant")
        ]
        return {
            "chat": conversations.summary(chat_id), "messages": messages,
            **self.get_chat_models(user_id, agent_id, chat_id),
        }

    def get_chat_models(self, user_id: str, agent_id: str, chat_id: str) -> dict:
        """Return effective models and overrides without reading conversation history."""
        conversations = self._conversations(user_id, agent_id)
        try:
            selection = conversations.model_selection(chat_id)
        except (FileNotFoundError, ValueError) as exc:
            raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from exc
        user = self._user(user_id)
        defaults = user_models(user, self._preferences)
        model = selection.model_dump() if selection is not None else {
            "provider": user.provider.name,
            "id": defaults.chat,
            "zdr": True,
        }
        agent_config = self._agent_config(user_id, agent_id).chat_config
        effort = self._reasoning_effort_override(conversations, chat_id)
        return {
            "model": model,
            "model_override": selection.model_dump() if selection else None,
            "stt_model": defaults.stt,
            "reasoning_effort": {
                "override": effort,
                "default": agent_config.reasoning_effort,
                "effective": effort or agent_config.reasoning_effort,
                "reasoning": agent_config.reasoning,
                "supported": self._reasoning_effort_supported(user, model["id"]),
            },
        }

    @staticmethod
    def _reasoning_effort_override(conversations: ConversationsSource, chat_id: str) -> ReasoningEffort | None:
        selection = conversations.reasoning_effort_selection(chat_id)
        return selection.effort if selection is not None else None

    def _reasoning_effort_supported(self, user: UserConfig, model_id: str) -> bool | None:
        """Whether the model advertises reasoning; None when the catalog cannot say."""
        if user.provider.name != "openrouter":
            return False
        cached = self._reasoning_support.get(model_id)
        if cached is not None and monotonic() - cached[0] < _REASONING_SUPPORT_TTL_SECONDS:
            return cached[1]
        try:
            models = list_openrouter_models(model_id, zdr=False)
        except CatalogUnavailable:
            return None
        model = next((item for item in models if item.id == model_id), None)
        if model is None:
            return None
        supported = any(
            "reasoning" in parameters or "reasoning_effort" in parameters
            for parameters in model.parameter_routes
        )
        self._reasoning_support[model_id] = (monotonic(), supported)
        return supported

    def set_chat_reasoning_effort(
        self, user_id: str, agent_id: str, chat_id: str, effort: ReasoningEffort | None,
    ) -> dict:
        """Persist or clear a chat's reasoning effort override, leaving other chats untouched."""
        conversations = self._conversations(user_id, agent_id)
        self._load(conversations, chat_id)
        if effort is not None:
            supported = self.get_chat_models(user_id, agent_id, chat_id)["reasoning_effort"]["supported"]
            if supported is None:
                raise InputError("reasoning effort support for this model could not be verified; try again")
            if supported is False:
                raise InputError("the selected model does not support reasoning effort")
        slot = self._slot(user_id, agent_id)
        with slot.state:
            if self._chat_busy(slot, chat_id):
                raise AgentBusy("wait for this chat's active and queued replies before changing reasoning effort")
            with slot.lock:
                try:
                    conversations.set_reasoning_effort(chat_id, effort)
                except FileNotFoundError as exc:
                    raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from exc
                slot.agents.pop(chat_id, None)
        return self.get_chat_models(user_id, agent_id, chat_id)

    def set_chat_model(
        self, user_id: str, agent_id: str, chat_id: str, model_id: str | None, *, zdr: bool = True,
    ) -> dict:
        """Persist an idle chat's model and routing policy without changing other chats."""
        conversations = self._conversations(user_id, agent_id)
        self._load(conversations, chat_id)
        if model_id is not None:
            models = self.list_models(user_id, model_id, zdr=zdr)
            if not any(model["id"] == model_id for model in models):
                raise InputError("model is not available in the OpenRouter chat catalog with the selected ZDR filter")
        slot = self._slot(user_id, agent_id)
        with slot.state:
            if self._chat_busy(slot, chat_id):
                raise AgentBusy("wait for this chat's active and queued replies before changing models")
            with slot.lock:
                try:
                    if model_id is None:
                        conversations.clear_model(chat_id)
                    else:
                        conversations.set_model(chat_id, model_id, zdr=zdr)
                except FileNotFoundError as exc:
                    raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from exc
                # an effort the new model does not support would be sent to it; drop it
                if self._reasoning_effort_override(conversations, chat_id) is not None:
                    effective = model_id or user_models(self._user(user_id), self._preferences).chat
                    if self._reasoning_effort_supported(self._user(user_id), effective) is False:
                        conversations.set_reasoning_effort(chat_id, None)
                slot.agents.pop(chat_id, None)
        return self.get_chat_models(user_id, agent_id, chat_id)["model"]

    def rename_chat(self, user_id: str, agent_id: str, chat_id: str, title: str) -> dict:
        """Replace a chat's title with a user-chosen one; automatic naming no longer changes it."""
        conversations = self._conversations(user_id, agent_id)
        self._load(conversations, chat_id)
        try:
            conversations.rename(chat_id, title)
        except FileNotFoundError as exc:
            raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from exc
        return conversations.summary(chat_id)

    def delete_chat(self, user_id: str, agent_id: str, chat_id: str) -> None:
        conversations = self._conversations(user_id, agent_id)
        slot = self._slot(user_id, agent_id)
        with slot.state:
            if self._chat_busy(slot, chat_id):
                raise AgentBusy("cannot delete a chat while it has an active or queued reply")
        with slot.lock:
            try:
                conversations.delete_chat(chat_id)
            except (FileNotFoundError, ValueError) as e:
                raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from e
            with slot.state:
                slot.agents.pop(chat_id, None)
        try:
            self._agent_index(user_id, agent_id).forget_chat(chat_id)
        except Exception:  # the chat is gone; a stale index entry is dropped by the next reconcile anyway
            logger.warning("could not drop the index chunks of deleted chat %s/%s/%s", user_id, agent_id, chat_id, exc_info=True)

    # ------------------------------------------------------------------ messaging

    def send(
        self, user_id: str, agent_id: str, chat_id: str, text: str,
        stream: Optional[bool] = None, attachments: Optional[list[dict]] = None,
    ) -> Iterator[ChatEvent]:
        """Send a user message to a chat and yield the reply as events.

        Validation and scheduling happen immediately, independently of event consumption.
        A new message interrupts an unfinished reply to the same chat and continues its turn.
        Other chats on the agent run independently. `stream=None` follows the agent's config.
        """
        conversations = self._conversations(user_id, agent_id)
        attachments = [ChatAttachment.model_validate(item).model_dump() for item in (attachments or [])]
        text, attachments, notice = self._decompose_videos(user_id, agent_id, chat_id, text, attachments)
        text, attachments = self._transcribe_audio(
            user_id, agent_id, chat_id, text, attachments,
        )
        text, attachments = self._extract_documents(text, attachments)
        if not text.strip() and not attachments:
            raise InputError("message is empty")
        slot = self._slot(user_id, agent_id)
        run = _MessageRun(chat_id, text, attachments, stream)
        if notice:
            run.events.put(ChatEvent("notice", text=notice, generation_id=run.generation_id))
        with slot.state:
            try:
                if not conversations.exists_chat(chat_id):
                    raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id)
            except ValueError as e:
                raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from e
            pending = slot.pending.setdefault(chat_id, deque())
            same_chat = list(range(len(pending)))
            active = slot.active.get(chat_id)
            if active is not None and active.chat_id == chat_id:
                run.continuation = active.cancellation.cancel()
            for index in same_chat:
                pending[index].cancellation.cancel()
                run.continuation = True
            if same_chat:
                pending.insert(same_chat[-1] + 1, run)
            elif run.continuation:
                pending.appendleft(run)
            else:
                pending.append(run)
            if chat_id not in slot.workers:
                worker = threading.Thread(
                    target=self._work, args=(user_id, agent_id, slot, chat_id), daemon=True,
                )
                slot.workers[chat_id] = worker
                worker.start()
        return self._events(run)

    @staticmethod
    def _transcription_text(transcription: str) -> str:
        return f"[AUDIO] Audio transcription:\n{transcription}"

    def _decompose_videos(
        self, user_id: str, agent_id: str, chat_id: str, text: str, attachments: list[dict],
    ) -> tuple[str, list[dict], str | None]:
        """Replace videos with sampled frames and an audio track when the chat model lacks video input.

        Models with image support receive the frames as images and the audio as audio attachments,
        which the transcription step then turns into text. Models without image support keep the
        video attachment so the agent rejects it with the usual modality error. The returned notice
        warns the user that the model sees only the sampled frames; it is None when nothing changed.
        """
        if not any(attachment["modality"] == "video" for attachment in attachments):
            return text, attachments, None
        conversations = self._conversations(user_id, agent_id)
        try:
            selection = conversations.model_selection(chat_id)
        except (FileNotFoundError, ValueError) as exc:
            raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from exc
        resolved = load_agent(user_id, agent_id)
        chat_model = selection.id if selection else user_models(self._user(user_id), self._preferences).chat
        supported = get_provider(resolved.provider_name, api_key=resolved.api_key).supported_modalities(chat_model)
        if "video" in supported or "image" not in supported:
            return text, attachments, None

        notes: list[str] = []
        expanded: list[dict] = []
        decomposed: list[VideoDecomposition] = []
        for attachment in attachments:
            if attachment["modality"] != "video":
                expanded.append(attachment)
                continue
            name = attachment["name"]
            try:
                data = base64.b64decode(attachment["data_url"].split(",", 1)[1], validate=True)
            except (IndexError, binascii.Error) as exc:
                raise InputError("invalid video data for '{name}'", name=name) from exc
            decomposition = decompose_video(data, name, VIDEO_DECOMPOSITION)
            expanded.extend(self._frame_attachment(name, index, frame)
                            for index, frame in enumerate(decomposition.frames, start=1))
            if decomposition.audio_wav is not None:
                expanded.append({
                    "modality": "audio",
                    "name": f"{name} audio",
                    "mime_type": "audio/wav",
                    "format": "wav",
                    "data_url": "data:audio/wav;base64," + base64.b64encode(decomposition.audio_wav).decode("ascii"),
                })
            decomposed.append(decomposition)
            notes.append(self._video_note(name, decomposition))
        text = "\n\n".join(part for part in [text.strip(), *notes] if part)
        return text, expanded, _decomposition_notice(self.language(user_id), decomposed)

    @staticmethod
    def _frame_attachment(name: str, index: int, frame: bytes) -> dict:
        return {
            "modality": "image",
            "name": f"{name} frame {index}",
            "mime_type": "image/jpeg",
            "format": "jpeg",
            "data_url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii"),
        }

    @staticmethod
    def _video_note(name: str, decomposition: VideoDecomposition) -> str:
        stamps = ", ".join(f"{seconds:g}s" for seconds in decomposition.timestamps)
        note = (
            f"[VIDEO] Video '{name}' was decomposed into {len(decomposition.frames)} frames, "
            f"attached in order at {stamps} of the video."
        )
        if decomposition.truncated:
            note += (
                f" Only the first {decomposition.sampled_seconds:g}s of the "
                f"{decomposition.duration:.1f}s video were sampled."
            )
        note += (
            " The audio track is transcribed below."
            if decomposition.audio_wav is not None
            else " The video has no audio track."
        )
        return note

    def _transcribe_audio(
        self, user_id: str, agent_id: str, chat_id: str, text: str, attachments: list[dict],
    ) -> tuple[str, list[dict]]:
        audio = [attachment for attachment in attachments if attachment["modality"] == "audio"]
        if not audio:
            return text, attachments
        self._require_chat(self._conversations(user_id, agent_id), chat_id)
        resolved = load_agent(user_id, agent_id)
        transcription_model = user_models(self._user(user_id), self._preferences).stt
        if resolved.provider_name != "openrouter":
            raise InputError("audio messages require an OpenRouter chat provider for speech-to-text")
        transcriber = OpenRouterProvider(api_key=resolved.api_key)
        transcripts = []
        remaining = []
        for attachment in attachments:
            if attachment["modality"] != "audio":
                remaining.append(attachment)
                continue
            try:
                encoded = attachment["data_url"].split(",", 1)[1]
            except IndexError as exc:
                raise InputError("invalid audio data for '{name}'", name=attachment["name"]) from exc
            transcript = transcriber.transcribe(
                encoded,
                attachment["format"],
                model=transcription_model,
                language=SPEECH_TO_TEXT.language,
                zdr=SPEECH_TO_TEXT.zdr,
            )
            if transcript:
                transcripts.append(transcript)
        if transcripts:
            text = "\n\n".join(part for part in [text.strip(), *(
                self._transcription_text(transcript) for transcript in transcripts
            )] if part)
        return text, remaining

    @staticmethod
    def _extract_documents(text: str, attachments: list[dict]) -> tuple[str, list[dict]]:
        """Replace PDF attachments with their page text, so providers only receive text."""
        documents = [attachment for attachment in attachments if attachment["modality"] == "document"]
        if not documents:
            return text, attachments
        remaining = [attachment for attachment in attachments if attachment["modality"] != "document"]
        blocks = [text.strip()] if text.strip() else []
        for attachment in documents:
            try:
                encoded = attachment["data_url"].split(",", 1)[1]
                data = base64.b64decode(encoded, validate=True)
            except (IndexError, binascii.Error) as exc:
                raise InputError("invalid PDF data for '{name}'", name=attachment["name"]) from exc
            extracted = extract_pdf_text(data, attachment["name"], PDF, lambda png: run_tesseract(png, PDF))
            blocks.append(f"Attached document: {attachment['name']}\n{extracted.render()}")
        return "\n\n".join(blocks), remaining

    @staticmethod
    def _events(run: _MessageRun) -> Iterator[ChatEvent]:
        while True:
            event = run.events.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield event

    def _work(self, user_id: str, agent_id: str, slot: _AgentSlot, chat_id: str) -> None:
        while True:
            with slot.state:
                pending = slot.pending.get(chat_id)
                if not pending:
                    slot.pending.pop(chat_id, None)
                    slot.workers.pop(chat_id, None)
                    slot.active.pop(chat_id, None)
                    return
                run = pending.popleft()
                slot.active[chat_id] = run
            try:
                for event in self._run(user_id, agent_id, slot, run):
                    run.events.put(event)
            except Exception as e:  # worker failures are delivered to the requesting channel
                logger.exception("reply failed for %s/%s/%s", user_id, agent_id, run.chat_id)
                run.events.put(e)
            finally:
                run.events.put(None)
                with slot.state:
                    slot.active.pop(chat_id, None)

    def _run(
        self, user_id: str, agent_id: str, slot: _AgentSlot, run: _MessageRun,
    ) -> Iterator[ChatEvent]:
        with slot.state:
            agent = slot.agents.get(run.chat_id)
            if agent is None:
                agent = self._agent_factory(user_id, agent_id, run.chat_id)
                slot.agents[run.chat_id] = agent
        agent.refresh_user_models(user_models(self._user(user_id), self._preferences))
        agent.config.language = self.language(user_id)
        try:
            result = agent.invoke(
                run.text, stream=run.stream, cancellation=run.cancellation,
                continuation=run.continuation, attachments=run.attachments,
            )
            if memory := agent.memory_event():  # a search that already finished (replies that are not streamed)
                yield ChatEvent("memory", data=memory, generation_id=run.generation_id)
            if isinstance(result, str):
                for action in agent.last_actions:
                    yield ChatEvent("tool", data=action, generation_id=run.generation_id)
                yield ChatEvent("text", result, generation_id=run.generation_id)
            else:
                with closing(result) as chunks:
                    for kind, chunk in chunks:
                        if kind in ("tool", "memory") and isinstance(chunk, dict):
                            yield ChatEvent(kind, data=chunk, generation_id=run.generation_id)
                        elif kind in ("reasoning", "content") and isinstance(chunk, str):
                            yield ChatEvent(
                                "reasoning" if kind == "reasoning" else "text",
                                chunk, generation_id=run.generation_id,
                            )
                        else:
                            raise ValueError(f"invalid agent event: {kind!r}")
        except GenerationCancelled:
            yield ChatEvent(
                "interrupted", agent.last_reply,
                {"reasoning": agent.last_reasoning, "actions": agent.last_actions},
                generation_id=run.generation_id,
            )
            return
        if agent.last_usage:
            report = agent.last_usage
            if agent.last_latency_report:
                report += "\n\n" + agent.last_latency_report
            yield ChatEvent("usage", report, generation_id=run.generation_id)
        self._name_chat(user_id, agent_id, run.chat_id)

    def _name_chat(self, user_id: str, agent_id: str, chat_id: str) -> None:
        """Generate a title once a chat reaches the configured length; request failures are logged, not raised."""
        conversations = self._conversations(user_id, agent_id)
        if conversations.generated_title(chat_id) is not None:
            return
        turns = [t for t in self._load(conversations, chat_id) if t.get("role") in ("user", "assistant")]
        if len(turns) < CHAT_NAMING.min_messages:
            return
        api_key = os.environ.get(CHAT_NAMING.api_key, "").strip()
        if not api_key:
            logger.warning("chat naming skipped: env var %s is not set", CHAT_NAMING.api_key)
            return
        messages = [
            {"role": "system", "content": _NAMING_PROMPT},
            {"role": "user", "content": _naming_transcript(turns)},
        ]
        try:
            reply = OpenRouterProvider(api_key=api_key).invoke_sync(
                messages, model=CHAT_NAMING.model, api_key=api_key, zdr=CHAT_NAMING.zdr,
            )
            title = conversations.set_generated_title(chat_id, reply["response"] or "")
        except Exception:
            logger.exception("chat naming failed for %s/%s/%s; keeping the first-message title", user_id, agent_id, chat_id)
            return
        logger.info("named chat %s/%s/%s: %s", user_id, agent_id, chat_id, title)

    # ------------------------------------------------------------------ dream mode and own files

    def dream_command(self, user_id: str, agent_id: str, text: str) -> str | None:
        """Answer `/dream`, `/dream start` or `/dream cancel` for the agent; None when `text` is not one."""
        command = parse_dream_command(text)
        if command is None:
            return None
        self._owned_agent(user_id, agent_id)
        language = self.language(user_id)
        if command == "invalid":
            return message(language, "Unknown /dream option. Use /dream for the status, /dream start to start a session or /dream cancel to stop the running one.")
        if command == "start":
            result = self._dream.start(user_id, agent_id, trigger="command")
            if result["status"] == "already_running":
                return message(language, "A dream session is already running for this agent.")
            if result["status"] == "started_nothing_pending":
                return message(language, "Nothing is pending; starting a dream session anyway.")
            return message(language, "Dream session started.")
        if command == "cancel":
            if self._dream.cancel(user_id, agent_id)["status"] == "not_running":
                return message(language, "No dream session is running for this agent.")
            return message(
                language, "Cancelling the dream session: a running tool call finishes first, then the session stops.",
            )
        return self._dream_status_text(language, self._dream.status(user_id, agent_id))

    @staticmethod
    def _dream_status_text(language: str, status: dict) -> str:
        labels = {
            "completed": message(language, "completed"),
            "budget_exhausted": message(language, "out of budget"),
            "failed": message(language, "failed"),
            "cancelled": message(language, "cancelled"),
            "timed_out": message(language, "timed out"),
        }
        lines = []
        if status["running"] and status["last_started_at"]:
            lines.append(message(
                language, "A dream session is running (started {when}).",
                when=format_local_time(status["last_started_at"]),
            ))
        last = status["last_session"]
        if last is None:
            lines.append(message(language, "No dream sessions yet."))
        else:
            lines.append(message(
                language, "Last dream session: {when} ({status}).",
                when=format_local_time(last["started_at"]), status=labels.get(last["status"], last["status"]),
            ))
            summary = " ".join((last.get("summary") or "").split())
            if summary:
                lines.append(summary if len(summary) <= 300 else summary[:299] + "…")
        schedule = status["schedule"]
        if schedule["every_hours"] is None and schedule["daily_at"] is None:
            lines.append(message(language, "Schedule: none (manual only)."))
        elif not schedule["enabled"]:
            lines.append(message(language, "Schedule: off (dream mode is disabled)."))
        elif schedule["every_hours"] is not None:
            lines.append(message(
                language, "Schedule: every {hours} h; next run {next}.",
                hours=schedule["every_hours"], next=format_local_time(schedule["next_run"]),
            ))
        else:
            lines.append(message(
                language, "Schedule: daily at {time}; next run {next}.",
                time=schedule["daily_at"], next=format_local_time(schedule["next_run"]),
            ))
        lines.append(message(language, "Pending chats: {count}.", count=status["pending_chats"]))
        if status["running"]:
            lines.append(message(language, "Use /dream cancel to stop the running session."))
        else:
            lines.append(message(language, "Use /dream start to start a new session."))
        return "\n".join(lines)

    def dream_status(self, user_id: str, agent_id: str) -> dict:
        self._owned_agent(user_id, agent_id)
        return self._dream.status(user_id, agent_id)

    def start_dream(self, user_id: str, agent_id: str) -> dict:
        """Start a dream session in the background; returns at once with its status and id."""
        self._owned_agent(user_id, agent_id)
        return self._dream.start(user_id, agent_id, trigger="web")

    def cancel_dream(self, user_id: str, agent_id: str) -> dict:
        """Ask the agent's running dream session to stop safely; returns at once (`cancelling | not_running`)."""
        self._owned_agent(user_id, agent_id)
        return self._dream.cancel(user_id, agent_id)

    def list_dream_sessions(self, user_id: str, agent_id: str) -> list[dict]:
        self._owned_agent(user_id, agent_id)
        return self._dream.sessions(user_id, agent_id)

    def get_dream_session(self, user_id: str, agent_id: str, session_id: str) -> dict:
        self._owned_agent(user_id, agent_id)
        try:
            return self._dream.session(user_id, agent_id, session_id)
        except FileNotFoundError as exc:
            raise UnknownDreamSession("unknown dream session: {session_id}", session_id=session_id) from exc

    def get_dream_settings(self, user_id: str, agent_id: str) -> dict:
        self._owned_agent(user_id, agent_id)
        config = self._agent_config(user_id, agent_id)
        return {
            "dream": config.dream.model_dump(mode="json"),
            "memory_limits": config.memory_limits.model_dump(mode="json"),
            "default_prompt": DEFAULT_DREAM_PROMPT,
        }

    def update_dream_settings(
        self, user_id: str, agent_id: str, dream: DreamConfig, memory_limits: MemoryLimits,
    ) -> dict:
        """Save the dream block and memory limits; idle cached chats pick them up when rebuilt."""
        self._owned_agent(user_id, agent_id)
        slot = self._slot(user_id, agent_id)
        with slot.lock:
            config = self._agent_config(user_id, agent_id)
            updated = config.model_copy(update={"dream": dream, "memory_limits": memory_limits})
            self._agents.update(user_id, agent_id, AgentConfig.model_validate(updated.model_dump()))
            with slot.state:
                for chat_id in [chat for chat in slot.agents if not self._chat_busy(slot, chat)]:
                    slot.agents.pop(chat_id, None)
        return self.get_dream_settings(user_id, agent_id)

    def get_soul(self, user_id: str, agent_id: str) -> dict:
        self._owned_agent(user_id, agent_id)
        content = self._agent_files(user_id, agent_id).soul_text()
        return {"content": content, "chars": len(content), "max_chars": SOUL_MAX_CHARS}

    def update_soul(self, user_id: str, agent_id: str, content: str) -> dict:
        """Replace SOUL.md (the previous version goes to the history); applies to new chats."""
        self._owned_agent(user_id, agent_id)
        self._agent_files(user_id, agent_id).write_soul(content)
        return self.get_soul(user_id, agent_id)

    def soul_history(self, user_id: str, agent_id: str) -> list[dict]:
        self._owned_agent(user_id, agent_id)
        return self._agent_files(user_id, agent_id).soul_history()

    def get_soul_version(self, user_id: str, agent_id: str, version: str) -> dict:
        self._owned_agent(user_id, agent_id)
        try:
            content = self._agent_files(user_id, agent_id).soul_version(version)
        except FileNotFoundError as exc:
            raise UnknownDreamSession("unknown SOUL version: {version}", version=version) from exc
        return {"version": version, "content": content}

    def restore_soul(self, user_id: str, agent_id: str, version: str) -> dict:
        """Make a saved SOUL version current; the replaced one is saved to the history first."""
        self._owned_agent(user_id, agent_id)
        try:
            self._agent_files(user_id, agent_id).restore_soul(version)
        except FileNotFoundError as exc:
            raise UnknownDreamSession("unknown SOUL version: {version}", version=version) from exc
        return self.get_soul(user_id, agent_id)

    def get_memory(self, user_id: str, agent_id: str, target: MemoryTarget) -> dict:
        self._owned_agent(user_id, agent_id)
        limits = self._agent_config(user_id, agent_id).memory_limits
        files = self._agent_files(user_id, agent_id)
        content = files.memory_text(target)
        return {
            "target": target, "content": content, "chars": len(content),
            "limit": limits.memory_chars if target == "memory" else limits.user_chars,
            "entries": files.memory_entries(target),
        }

    def update_memory(self, user_id: str, agent_id: str, target: MemoryTarget, content: str) -> dict:
        """Replace MEMORY.md or USER.md; entries are separated by a line holding only `§`."""
        self._owned_agent(user_id, agent_id)
        limits = self._agent_config(user_id, agent_id).memory_limits
        limit = limits.memory_chars if target == "memory" else limits.user_chars
        if len(content) > limit:
            raise InputError(
                "the {target} memory cannot exceed {limit} characters; shorten it or raise memory_limits",
                target=target, limit=limit,
            )
        files = self._agent_files(user_id, agent_id)
        with files.memory_lock():
            files.write_memory_text(target, content)
        return self.get_memory(user_id, agent_id, target)

    # ------------------------------------------------------------------ vector index

    def index_status(self, user_id: str, agent_id: str) -> dict:
        """The agent's index: sources, files, chats and chunks indexed, last reconciles, rebuilding or busy."""
        return self._agent_index(user_id, agent_id).status()

    def reindex(self, user_id: str, agent_id: str, *, full: bool = False) -> dict:
        """Queue a reconcile of every source now (folders included); `full` rebuilds the index from scratch."""
        index = self._agent_index(user_id, agent_id)
        index.reindex(full=full)
        return index.status()

    def _agent_index(self, user_id: str, agent_id: str) -> VectorIndex:
        self._owned_agent(user_id, agent_id)
        config = load_agent(user_id, agent_id)
        sources = SourceRegistry.for_agent(config.user_id, config.id, **chat_folder_options(config))
        return self._index_factory(config, sources)

    @staticmethod
    def _check_folders(folders: list[FolderConfig]) -> None:
        for folder in folders:
            if not Path(folder.path).expanduser().is_dir():
                raise InputError(
                    "folder '{name}' not found: {path}; fix `folders` in the agent configuration",
                    name=folder.name, path=folder.path,
                )

    def _agent_files(self, user_id: str, agent_id: str) -> AgentFilesSource:
        return SourceRegistry.for_agent(user_id, agent_id).agent_files()

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _kept_chats(slot: _AgentSlot, keep: str | None = None) -> set[str]:
        kept = set(slot.pending) | set(slot.active)
        if keep is not None:
            kept.add(keep)
        return kept

    @staticmethod
    def _chat_busy(slot: _AgentSlot, chat_id: str) -> bool:
        return chat_id in slot.active or bool(slot.pending.get(chat_id))

    @staticmethod
    def _check_id(kind: str, value: str, error: type[ServiceError]) -> None:
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise error("invalid {kind} id: {value!r}", kind=kind, value=value)

    def _user(self, user_id: str) -> UserConfig:
        self._check_id("user", user_id, UnknownUser)
        try:
            return load_user(user_id)
        except FileNotFoundError as e:
            raise UnknownUser("unknown user: {user_id}", user_id=user_id) from e

    def _owned_agent(self, user_id: str, agent_id: str) -> UserConfig:
        """The user's config, after checking that the user owns the agent."""
        user = self._user(user_id)
        self._check_id("agent", agent_id, UnknownAgent)
        if agent_id not in user.agents:
            raise UnknownAgent(
                "agent '{agent_id}' is not owned by user '{user_id}'", agent_id=agent_id, user_id=user_id,
            )
        return user

    def _conversations(self, user_id: str, agent_id: str) -> ConversationsSource:
        """The conversations source of an agent, after checking that the user owns it."""
        self._owned_agent(user_id, agent_id)
        return SourceRegistry.for_agent(user_id, agent_id).conversations()

    @staticmethod
    def _require_chat(conversations: ConversationsSource, chat_id: str) -> None:
        try:
            exists = conversations.exists_chat(chat_id)
        except ValueError as exc:
            raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from exc
        if not exists:
            raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id)

    @staticmethod
    def _load(conversations: ConversationsSource, chat_id: str) -> list[dict]:
        try:
            return conversations.load(chat_id)
        except (FileNotFoundError, ValueError) as e:
            raise UnknownChat("unknown chat: {chat_id}", chat_id=chat_id) from e

    def _slot(self, user_id: str, agent_id: str) -> _AgentSlot:
        with self._slots_guard:
            return self._slots.setdefault((user_id, agent_id), _AgentSlot())
