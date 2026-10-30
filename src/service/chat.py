from __future__ import annotations

import re
import threading
from collections import deque
from contextlib import closing
from dataclasses import dataclass, field
from queue import SimpleQueue
from typing import Callable, Iterator, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from src.agent.agent import Agent
from src.config.favorites import ModelFavorites
from src.config.loader import load_agent, load_agent_config, load_user
from src.config.models import UserConfig
from src.config.settings import CONFIG_DIR, SPEECH_TO_TEXT
from src.core.sources.conversations import ConversationsSource
from src.core.sources.registry import SourceRegistry
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.catalog import list_openrouter_models
from src.providers.catalog import list_openrouter_transcription_models
from src.providers.openrouter import OpenRouterProvider
from src.utils.logger import get_logger

logger = get_logger("service")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

AgentFactory = Callable[[str, str, Optional[str]], Agent]


class ServiceError(Exception):
    """Base error of the chat service; channels translate subclasses to their own responses."""


class UnknownUser(ServiceError):
    pass


class UnknownAgent(ServiceError):
    pass


class UnknownChat(ServiceError):
    pass


class AgentBusy(ServiceError):
    pass


class ChatAttachment(BaseModel):
    """A validated data URL attached to a user message."""

    model_config = ConfigDict(extra="forbid")

    modality: Literal["image", "video", "audio"]
    name: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=128)
    format: str = Field(min_length=1, max_length=16)
    data_url: str = Field(min_length=32, max_length=30_000_000)


@dataclass(frozen=True)
class ChatEvent:
    """One piece of an agent reply, independent of the channel that delivers it.

    `kind` is `reasoning` (thinking tokens), `text` (answer tokens), `tool` (an action the agent
    took, in `data`: name, arguments, result, error) or `usage` (context report, emitted once
    after the reply) or `interrupted` (partial answer in `text`, reasoning/actions in `data`).
    Non-streaming replies arrive as `tool` events followed by one `text` event. All events
    carry a generation id so adapters can distinguish overlapping deliveries.
    """

    kind: Literal["reasoning", "text", "tool", "usage", "interrupted", "memory"]
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
        self.agent: Optional[Agent] = None
        self.lock = threading.Lock()
        self.state = threading.Lock()
        self.pending: deque[_MessageRun] = deque()
        self.active: _MessageRun | None = None
        self.worker: threading.Thread | None = None


class ChatService:
    """Channel-agnostic chat operations for a user: agents, chats and sending messages.

    Every call is scoped to a `user_id` that the channel has already resolved from its own
    identity (web login, telegram id, ...). Returns plain dicts and events, never internal objects.
    """

    def __init__(self, agent_factory: AgentFactory = Agent.from_config):
        self._agent_factory = agent_factory
        self._slots: dict[tuple[str, str], _AgentSlot] = {}
        self._slots_guard = threading.Lock()
        self._favorites = ModelFavorites(CONFIG_DIR)

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

    def list_agents(self, user_id: str) -> list[dict]:
        user = self._user(user_id)
        agents = []
        for agent_id in user.agents:
            cfg = load_agent_config(user_id, agent_id)
            agents.append({"id": cfg.id, "name": cfg.name})
        return agents

    def list_models(self, user_id: str, query: str = "", *, zdr: bool = True) -> list[dict]:
        """List searchable chat models for an OpenRouter user, requiring ZDR by default."""
        user = self._user(user_id)
        if user.provider.name != "openrouter":
            raise ValueError("model browsing is only available for the OpenRouter chat provider")
        return [model.model_dump() for model in list_openrouter_models(query, zdr=zdr)]

    def list_transcription_models(self, user_id: str, query: str = "", *, zdr: bool = True) -> list[dict]:
        """List OpenRouter speech-to-text models for the current user."""
        user = self._user(user_id)
        if user.provider.name != "openrouter":
            raise ValueError("speech-to-text model browsing requires the OpenRouter chat provider")
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
            raise ValueError("model is not available in the OpenRouter chat catalog with the selected ZDR filter")
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
                    "associative_matches", "usage", "usage_report",
                )
                if t.get(k) is not None
            }
            for t in turns
            if t.get("role") in ("user", "assistant")
        ]
        selection = conversations.model_selection(chat_id)
        model = selection.model_dump() if selection is not None else {
            "provider": self._user(user_id).provider.name,
            "id": load_agent_config(user_id, agent_id).models.chat,
            "zdr": True,
        }
        return {"chat": conversations.summary(chat_id), "messages": messages, "model": model}

    def set_chat_model(
        self, user_id: str, agent_id: str, chat_id: str, model_id: str, *, zdr: bool = True,
    ) -> dict:
        """Persist an idle chat's model and routing policy without changing other chats."""
        conversations = self._conversations(user_id, agent_id)
        self._load(conversations, chat_id)
        models = self.list_models(user_id, model_id, zdr=zdr)
        if not any(model["id"] == model_id for model in models):
            raise ValueError("model is not available in the OpenRouter chat catalog with the selected ZDR filter")
        slot = self._slot(user_id, agent_id)
        with slot.state:
            if slot.active is not None or slot.pending:
                raise AgentBusy("wait for this agent's active and queued replies before changing models")
            with slot.lock:
                try:
                    selection = conversations.set_model(chat_id, model_id, zdr=zdr)
                except FileNotFoundError as exc:
                    raise UnknownChat(f"unknown chat: {chat_id}") from exc
                if slot.agent is not None and slot.agent.chat_id == chat_id:
                    slot.agent = None
        return selection.model_dump()

    def delete_chat(self, user_id: str, agent_id: str, chat_id: str) -> None:
        conversations = self._conversations(user_id, agent_id)
        slot = self._slot(user_id, agent_id)
        with slot.lock:  # wait for any reply in flight on this agent
            try:
                conversations.delete_chat(chat_id)
            except (FileNotFoundError, ValueError) as e:
                raise UnknownChat(f"unknown chat: {chat_id}") from e
            if slot.agent is not None and slot.agent.chat_id == chat_id:
                slot.agent = None  # rebuilt on next use rather than left pointing at a deleted chat

    # ------------------------------------------------------------------ messaging

    def send(
        self, user_id: str, agent_id: str, chat_id: str, text: str,
        stream: Optional[bool] = None, attachments: Optional[list[dict]] = None,
        transcription_model: Optional[str] = None,
    ) -> Iterator[ChatEvent]:
        """Send a user message to a chat and yield the reply as events.

        Validation and scheduling happen immediately, independently of event consumption.
        A new message interrupts an unfinished reply to the same chat and continues its turn.
        Other chats on the agent remain serialized. `stream=None` follows the agent's config.
        """
        conversations = self._conversations(user_id, agent_id)
        attachments = [ChatAttachment.model_validate(item).model_dump() for item in (attachments or [])]
        text, attachments = self._transcribe_audio(
            user_id, agent_id, text, attachments, transcription_model,
        )
        if not text.strip() and not attachments:
            raise ValueError("message is empty")
        slot = self._slot(user_id, agent_id)
        run = _MessageRun(chat_id, text, attachments, stream)
        with slot.state:
            try:
                if not conversations.exists_chat(chat_id):
                    raise UnknownChat(f"unknown chat: {chat_id}")
            except ValueError as e:
                raise UnknownChat(f"unknown chat: {chat_id}") from e
            same_chat = [
                i for i, queued in enumerate(slot.pending) if queued.chat_id == chat_id
            ]
            active = slot.active
            if active is not None and active.chat_id == chat_id:
                run.continuation = active.cancellation.cancel()
            for index in same_chat:
                slot.pending[index].cancellation.cancel()
                run.continuation = True
            if same_chat:
                slot.pending.insert(same_chat[-1] + 1, run)
            elif run.continuation:
                slot.pending.appendleft(run)
            else:
                slot.pending.append(run)
            if slot.worker is None:
                slot.worker = threading.Thread(
                    target=self._work, args=(user_id, agent_id, slot), daemon=True,
                )
                slot.worker.start()
        return self._events(run)

    @staticmethod
    def _transcription_text(transcription: str) -> str:
        return f"[Audio transcription]\n{transcription}"

    def _transcribe_audio(
        self, user_id: str, agent_id: str, text: str, attachments: list[dict],
        transcription_model: str | None = None,
    ) -> tuple[str, list[dict]]:
        audio = [attachment for attachment in attachments if attachment["modality"] == "audio"]
        if not audio:
            return text, attachments
        resolved = load_agent(user_id, agent_id)
        if resolved.provider_name != "openrouter":
            raise ValueError("audio messages require an OpenRouter chat provider for speech-to-text")
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
                raise ValueError(f"invalid audio data for '{attachment['name']}'") from exc
            transcript = transcriber.transcribe(
                encoded,
                attachment["format"],
                model=transcription_model or SPEECH_TO_TEXT.model,
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
    def _events(run: _MessageRun) -> Iterator[ChatEvent]:
        while True:
            event = run.events.get()
            if event is None:
                return
            if isinstance(event, Exception):
                raise event
            yield event

    def _work(self, user_id: str, agent_id: str, slot: _AgentSlot) -> None:
        while True:
            with slot.state:
                if not slot.pending:
                    slot.worker = None
                    slot.active = None
                    return
                run = slot.pending.popleft()
                slot.active = run
            try:
                for event in self._run(user_id, agent_id, slot, run):
                    run.events.put(event)
            except Exception as e:  # worker failures are delivered to the requesting channel
                logger.exception("reply failed for %s/%s/%s", user_id, agent_id, run.chat_id)
                run.events.put(e)
            finally:
                run.events.put(None)
                with slot.state:
                    slot.active = None

    def _run(
        self, user_id: str, agent_id: str, slot: _AgentSlot, run: _MessageRun,
    ) -> Iterator[ChatEvent]:
        with slot.lock:
            if slot.agent is None:
                slot.agent = self._agent_factory(user_id, agent_id, run.chat_id)
            elif slot.agent.chat_id != run.chat_id:
                slot.agent.open_chat(run.chat_id)
            agent = slot.agent
            try:
                result = agent.invoke(
                    run.text, stream=run.stream, cancellation=run.cancellation,
                    continuation=run.continuation, attachments=run.attachments,
                )
                if agent._assoc_match_count:
                    yield ChatEvent(
                        "memory", data={"matches": agent._assoc_match_count},
                        generation_id=run.generation_id,
                    )
                if isinstance(result, str):
                    for action in agent.last_actions:
                        yield ChatEvent("tool", data=action, generation_id=run.generation_id)
                    yield ChatEvent("text", result, generation_id=run.generation_id)
                else:
                    with closing(result) as chunks:
                        for kind, chunk in chunks:
                            if kind == "tool" and isinstance(chunk, dict):
                                yield ChatEvent("tool", data=chunk, generation_id=run.generation_id)
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

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _kept_chats(slot: _AgentSlot, keep: str | None = None) -> set[str]:
        kept = {run.chat_id for run in slot.pending}
        if slot.active is not None:
            kept.add(slot.active.chat_id)
        if keep is not None:
            kept.add(keep)
        return kept

    @staticmethod
    def _check_id(kind: str, value: str, error: type[ServiceError]) -> None:
        if not isinstance(value, str) or not _ID_RE.match(value):
            raise error(f"invalid {kind} id: {value!r}")

    def _user(self, user_id: str) -> UserConfig:
        self._check_id("user", user_id, UnknownUser)
        try:
            return load_user(user_id)
        except FileNotFoundError as e:
            raise UnknownUser(f"unknown user: {user_id}") from e

    def _conversations(self, user_id: str, agent_id: str) -> ConversationsSource:
        """The conversations source of an agent, after checking that the user owns it."""
        user = self._user(user_id)
        self._check_id("agent", agent_id, UnknownAgent)
        if agent_id not in user.agents:
            raise UnknownAgent(f"agent '{agent_id}' is not owned by user '{user_id}'")
        return SourceRegistry.for_agent(user_id, agent_id).conversations()

    @staticmethod
    def _load(conversations: ConversationsSource, chat_id: str) -> list[dict]:
        try:
            return conversations.load(chat_id)
        except (FileNotFoundError, ValueError) as e:
            raise UnknownChat(f"unknown chat: {chat_id}") from e

    def _slot(self, user_id: str, agent_id: str) -> _AgentSlot:
        with self._slots_guard:
            return self._slots.setdefault((user_id, agent_id), _AgentSlot())
