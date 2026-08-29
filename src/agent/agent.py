from __future__ import annotations

from typing import Iterator, Optional, Union

import src.providers.openrouter  # noqa: F401  (registers the openrouter provider)
from src.config.loader import load_agent
from src.config.models import ResolvedAgent
from src.providers.provider import LLMProvider, get_provider


class Agent:
    """Orchestration entry point: wraps an agent's config, provider and conversation memory."""

    def __init__(self, config: ResolvedAgent, provider: LLMProvider):
        self.config = config
        self.provider = provider
        # conversation history is the memory
        self.messages: list[dict] = [
            {"role": "system", "content": config.personality_prompt}
        ]

    @classmethod
    def from_config(cls, user_id: str, agent_id: Optional[str] = None) -> "Agent":
        config = load_agent(user_id, agent_id)
        provider = get_provider(config.provider_name, api_key=config.api_key)
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
        # without memory, each turn starts fresh from the system prompt
        if not self.config.has_memory:
            self.messages = [{"role": "system", "content": self.config.personality_prompt}]
        self.messages.append({"role": "user", "content": user_input})
        return self._stream() if stream else self._complete()

    def _stream(self) -> Iterator[str]:
        reply = ""
        for chunk in self.provider.invoke(self.messages, self.config.models.chat, stream=True):
            if isinstance(chunk, str):
                reply += chunk
                yield chunk
        self.messages.append({"role": "assistant", "content": reply})

    def _complete(self) -> str:
        result = self.provider.invoke(self.messages, self.config.models.chat, stream=False)
        reply = result["response"]
        self.messages.append({"role": "assistant", "content": reply})
        return reply
