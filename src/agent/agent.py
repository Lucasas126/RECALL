from __future__ import annotations

from typing import Iterator, Optional, Union

import src.providers.openrouter  # noqa: F401  (registers the openrouter provider)
import src.providers.ollama  # noqa: F401  (registers the ollama provider)
from src.config.loader import load_agent
from src.config.models import ResolvedAgent
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
        # conversation history is the memory
        self.messages: list[dict] = [
            {"role": "system", "content": config.personality_prompt}
        ]
        # thinking tokens from the last reply, kept apart from the answer
        self.last_reasoning: str = ""
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
        self.messages.append({"role": "user", "content": user_input})
        self._log_turn("user", user_input)
        return self._stream() if stream else self._complete()

    def _stream(self) -> Iterator[tuple]:
        reasoning, reply = "", ""
        for chunk in self.provider.invoke(
            self.messages, self.config.models.chat,
            stream=True, reasoning=self.config.reasoning, reasoning_tokens=self.config.reasoning_tokens,
        ):
            if isinstance(chunk, tuple):
                kind, text = chunk
                if kind == "reasoning":
                    reasoning += text
                else:
                    reply += text
                yield chunk
        self.last_reasoning = reasoning
        self._remember_reply(reply, reasoning)

    def _complete(self) -> str:
        result = self.provider.invoke(
            self.messages, self.config.models.chat,
            stream=False, reasoning=self.config.reasoning, reasoning_tokens=self.config.reasoning_tokens,
        )
        self.last_reasoning = result.get("reasoning", "")
        reply = result["response"]
        self._remember_reply(reply, self.last_reasoning)
        return reply

    def _remember_reply(self, reply: str, reasoning: str) -> None:
        """Append the assistant reply to memory, optionally keeping the thinking with it."""
        message = {"role": "assistant", "content": reply}
        if self.config.keep_reasoning_in_memory and reasoning:
            message["reasoning"] = reasoning
        self.messages.append(message)
        self._log_turn("assistant", reply, reasoning=reasoning or None)

    def _log_turn(self, role: str, content: str, **extra) -> None:
        """Persist a turn to the conversation jsonl (independent of in-context memory)."""
        extra = {k: v for k, v in extra.items() if v is not None}
        self.sources.conversations().append(role, content, **extra)
