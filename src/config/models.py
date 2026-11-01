from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.agent.tool_groups import validate_tool_groups
ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]


class TavilyConfig(BaseModel):
    """Global web access settings; api_key names an environment variable, not a secret."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(default="TAVILY_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    timeout_seconds: float = Field(default=30, ge=1, le=60, allow_inf_nan=False)
    max_content_chars: int = Field(default=16000, ge=1000, le=100000, strict=True)


class ChatNamingConfig(BaseModel):
    """Framework-wide small model that generates chat titles; api_key names an environment variable, not a secret."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(default="openai/gpt-4o-mini", strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    zdr: bool = Field(default=True, strict=True)
    min_messages: int = Field(default=5, ge=1, le=1000, strict=True)


class SpeechToTextConfig(BaseModel):
    """OpenRouter speech-to-text settings."""

    zdr: bool = Field(default=True, strict=True)
    language: Optional[str] = Field(default=None, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")


class VideoDecompositionConfig(BaseModel):
    """Frame sampling limits for videos sent to models without native video input."""

    model_config = ConfigDict(extra="forbid")

    frame_interval_seconds: float = Field(default=5, gt=0, le=60, allow_inf_nan=False)
    max_frames: int = Field(default=16, ge=1, le=64, strict=True)
    max_width: int = Field(default=768, ge=64, le=4096, strict=True)


class ProviderConfig(BaseModel):
    name: str
    api_key: Optional[str] = None  # name of the env var holding the actual key (not needed for local providers)


class ModelsConfig(BaseModel):
    """User-owned inference and indexing model defaults."""

    model_config = ConfigDict(extra="forbid")

    chat: str = Field(default="openai/gpt-4o-mini", strict=True, min_length=1, max_length=256)
    stt: str = Field(default="openai/whisper-large-v3-turbo", strict=True, min_length=1, max_length=256)
    embeddings: str = Field(default="openai/text-embedding-3-small", strict=True, min_length=1, max_length=256)


class UserConfig(BaseModel):
    id: str
    name: str
    password_hash: Optional[str] = Field(default=None, min_length=1)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    provider: ProviderConfig  # chat provider
    embeddings_provider: Optional[ProviderConfig] = None  # falls back to `provider`
    agents: list[str]
    models: ModelsConfig = Field(default_factory=ModelsConfig)


class ChatConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    stream: bool = True
    has_memory: bool = True
    reasoning: bool = False
    # effort level for models that support it (minimal|low|medium|high|xhigh|max); openrouter maps it
    reasoning_effort: Optional[str] = "medium"
    reasoning_tokens: Optional[int] = None
    keep_reasoning_in_memory: bool = False
    # last N conversation messages kept in context (None = all); system prompt is always kept
    conversation_messages_retained: Optional[int] = 30
    # let the model call tools (memory recall, source read/write/list)
    tools: bool = True
    tool_groups: list[str] | None = None
    allow: list[str] | None = None
    deny: list[str] | None = None
    # run a memory subagent each turn that suggests relevant fragments from long-term memory
    associative_memory: bool = True

    @field_validator("tool_groups")
    @classmethod
    def _validate_tool_groups(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else validate_tool_groups(value)


class AgentConfig(BaseModel):
    """One agent's own configuration; `id` identifies the agent and its data folder."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    personality_prompt: str = Field(min_length=1, max_length=20000)
    chat_config: ChatConfig = ChatConfig()


class ResolvedAgent(BaseModel):
    """Runtime dependencies resolved from the user's models/providers and agent behavior."""

    user_id: str
    language: str = "en"
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
    reasoning_effort: Optional[str] = None
    reasoning_tokens: Optional[int] = None
    keep_reasoning_in_memory: bool = False
    conversation_messages_retained: Optional[int] = None
    tools: bool = True
    tool_groups: list[str] | None = None
    allow: list[str] | None = None
    deny: list[str] | None = None
    associative_memory: bool = True