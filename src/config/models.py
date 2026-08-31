from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ProviderConfig(BaseModel):
    name: str
    api_key: Optional[str] = None  # name of the env var holding the actual key (not needed for local providers)


class UserConfig(BaseModel):
    id: str
    name: str
    provider: ProviderConfig  # chat provider
    embeddings_provider: Optional[ProviderConfig] = None  # falls back to `provider`
    agents: list[str]


class ModelsConfig(BaseModel):
    chat: str = "openai/gpt-4o-mini"
    embeddings: str = "openai/text-embedding-3-small"


class ChatConfig(BaseModel):
    stream: bool = True
    has_memory: bool = True
    reasoning: bool = False
    reasoning_tokens: Optional[int] = None
    keep_reasoning_in_memory: bool = False
    # last N conversation messages kept in context (None = all); system prompt is always kept
    conversation_messages_retained: Optional[int] = None


class AgentConfig(BaseModel):
    id: str
    name: str
    personality_prompt: str
    models: ModelsConfig = ModelsConfig()
    chat_config: ChatConfig = ChatConfig()


class ResolvedAgent(BaseModel):
    """An agent with its config merged and references (like the provider key) resolved."""

    user_id: str
    id: str
    name: str
    personality_prompt: str
    models: ModelsConfig
    provider_name: str
    api_key: str
    embeddings_provider_name: str
    embeddings_api_key: str
    stream: bool = True
    has_memory: bool = True
    reasoning: bool = False
    reasoning_tokens: Optional[int] = None
    keep_reasoning_in_memory: bool = False
    conversation_messages_retained: Optional[int] = None
