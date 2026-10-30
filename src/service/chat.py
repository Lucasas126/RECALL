from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Callable, Iterator, Literal, Optional

from src.agent.agent import Agent
from src.config.loader import load_agent_config, load_user
from src.config.models import UserConfig
from src.core.sources.conversations import ConversationsSource
from src.core.sources.registry import SourceRegistry
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


@dataclass(frozen=True)
class ChatEvent:
    """One piece of an agent reply, independent of the channel that delivers it.

    `kind` is `reasoning` (thinking tokens), `text` (answer tokens), `tool` (an action the agent
    took, in `data`: name, arguments, result, error) or `usage` (context report, emitted once
    after the reply). Non-streaming replies arrive as `tool` events followed by one `text` event.
    """

    kind: Literal["reasoning", "text", "tool", "usage"]
    text: str = ""
    data: Optional[dict] = None


class _AgentSlot:
    """A lazily built agent plus the lock that serializes access to it (one active chat at a time)."""

    def __init__(self) -> None:
        self.agent: Optional[Agent] = None
        self.lock = threading.Lock()


class ChatService:
    """Channel-agnostic chat operations for a user: agents, chats and sending messages.

    Every call is scoped to a `user_id` that the channel has already resolved from its own
    identity (web login, telegram id, ...). Returns plain dicts and events, never internal objects.
    """

    def __init__(self, agent_factory: AgentFactory = Agent.from_config):
        self._agent_factory = agent_factory
        self._slots: dict[tuple[str, str], _AgentSlot] = {}
        self._slots_guard = threading.Lock()

    # ------------------------------------------------------------------ users & agents

    def login(self, user_id: str) -> dict:
        """Resolve an existing user and return their profile and agents; raises UnknownUser."""
        user = self._user(user_id)
        return {"id": user_id, "name": user.name, "agents": self.list_agents(user_id)}

    def list_agents(self, user_id: str) -> list[dict]:
        user = self._user(user_id)
        agents = []
        for agent_id in user.agents:
            cfg = load_agent_config(user_id, agent_id)
            agents.append({"id": cfg.id, "name": cfg.name})
        return agents

    # ------------------------------------------------------------------ chats

    def list_chats(self, user_id: str, agent_id: str, keep: Optional[str] = None) -> list[dict]:
        """Chat summaries, most recently active first; unused empty chats (except `keep`) are removed."""
        conversations = self._conversations(user_id, agent_id)
        conversations.prune_empty(keep)
        summaries = [conversations.summary(chat_id) for chat_id in conversations.chats()]
        return sorted(summaries, key=lambda s: s["updated"], reverse=True)

    def create_chat(self, user_id: str, agent_id: str) -> dict:
        """Create a chat, discarding any other unused empty ones so they never pile up."""
        conversations = self._conversations(user_id, agent_id)
        conversations.prune_empty()
        return conversations.summary(conversations.create())

    def get_chat(self, user_id: str, agent_id: str, chat_id: str) -> dict:
        """A chat's summary and its user/assistant messages."""
        conversations = self._conversations(user_id, agent_id)
        turns = self._load(conversations, chat_id)
        messages = [
            {k: t[k] for k in ("role", "content", "ts", "reasoning", "actions") if t.get(k) is not None}
            for t in turns
            if t.get("role") in ("user", "assistant")
        ]
        return {"chat": conversations.summary(chat_id), "messages": messages}

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
        self, user_id: str, agent_id: str, chat_id: str, text: str, stream: Optional[bool] = None
    ) -> Iterator[ChatEvent]:
        """Send a user message to a chat and yield the reply as events.

        Validation happens immediately; the agent runs as the iterator is consumed. Messages to
        the same agent are serialized. `stream=None` follows the agent's config.
        """
        conversations = self._conversations(user_id, agent_id)
        self._load(conversations, chat_id)  # raises UnknownChat
        if not text.strip():
            raise ValueError("message is empty")
        return self._run(user_id, agent_id, chat_id, text, stream)

    def _run(
        self, user_id: str, agent_id: str, chat_id: str, text: str, stream: Optional[bool]
    ) -> Iterator[ChatEvent]:
        slot = self._slot(user_id, agent_id)
        with slot.lock:
            if slot.agent is None:
                slot.agent = self._agent_factory(user_id, agent_id, chat_id)
            elif slot.agent.chat_id != chat_id:
                slot.agent.open_chat(chat_id)
            agent = slot.agent
            result = agent.invoke(text, stream=stream)
            if isinstance(result, str):
                for action in agent.last_actions:
                    yield ChatEvent("tool", data=action)
                yield ChatEvent("text", result)
            else:
                for kind, chunk in result:
                    if kind == "tool":
                        yield ChatEvent("tool", data=chunk)
                    else:
                        yield ChatEvent("reasoning" if kind == "reasoning" else "text", chunk)
            if agent.last_usage:
                yield ChatEvent("usage", agent.last_usage)

    # ------------------------------------------------------------------ internals

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
