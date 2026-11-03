from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, field_validator, model_validator

from src.agent.tool_groups import validate_tool_groups
ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
# a bare file name inside the user's private MCP folder; no separators, so it cannot escape it
_STATE_FILE = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
McpServerId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


def _legacy_associative_mode(value: object) -> object:
    """Accept the former on/off boolean: true means `llm`, false means `off`."""
    if isinstance(value, bool):
        return "llm" if value else "off"
    return value


# how an agent searches its memory each turn: not at all, with a query proposed by a small model, or with the user's message
AssociativeMode = Annotated[Literal["off", "llm", "question_only"], BeforeValidator(_legacy_associative_mode)]
# whether the search runs beside the model (and may pause its reply) or finishes before the model starts
AssociativeExecution = Literal["parallel", "sequential"]


class McpEnvFromEnvironment(BaseModel):
    """Environment value copied from RECALL's own environment, shared by every user."""

    model_config = ConfigDict(extra="forbid")

    env: str = Field(pattern=_ENV_NAME)


class McpEnvUserFile(BaseModel):
    """Absolute path of a file in the requesting user's private folder for the server."""

    model_config = ConfigDict(extra="forbid")

    user_file: str = Field(pattern=_STATE_FILE)


McpEnvValue = Union[str, McpEnvFromEnvironment, McpEnvUserFile]


class McpAuthConfig(BaseModel):
    """Interactive sign-in run by the server's own command; it stores the user's credentials."""

    model_config = ConfigDict(extra="forbid")

    args: list[str] = Field(min_length=1)
    credential_file: str = Field(pattern=_STATE_FILE)
    url_pattern: str = Field(default=r"Authentication URL:\s*(https?://\S+)", min_length=1)
    timeout_seconds: int = Field(default=300, ge=30, le=1800, strict=True)


class McpServerConfig(BaseModel):
    """One MCP server offered to every user; each user connects with their own credentials."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    transport: Literal["stdio"] = "stdio"
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    env: dict[str, McpEnvValue] = Field(default_factory=dict)
    exclude_tools: list[str] = Field(default_factory=list)
    auth: Optional[McpAuthConfig] = None

    @field_validator("env")
    @classmethod
    def _validate_env_names(cls, value: dict[str, McpEnvValue]) -> dict[str, McpEnvValue]:
        invalid = sorted(name for name in value if not re.match(_ENV_NAME, name))
        if invalid:
            raise ValueError(f"invalid environment variable name: {', '.join(invalid)}")
        return value


class McpSettings(BaseModel):
    """Framework-wide MCP server catalog and client limits."""

    model_config = ConfigDict(extra="forbid")

    connect_timeout_seconds: float = Field(default=120, ge=1, le=600, allow_inf_nan=False)
    call_timeout_seconds: float = Field(default=60, ge=1, le=600, allow_inf_nan=False)
    servers: dict[McpServerId, McpServerConfig] = Field(default_factory=dict)


class AgentMcpConfig(BaseModel):
    """An agent's use of one MCP server; the server is enabled while this entry exists."""

    model_config = ConfigDict(extra="forbid")

    tools: Optional[list[str]] = None  # None exposes every tool the server offers
    read_only: bool = False
    instructions: str = Field(default="", max_length=4000)

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: Optional[list[str]]) -> Optional[list[str]]:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("MCP tool selections must not contain duplicates")
        return value


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


class PromptCacheConfig(BaseModel):
    """Framework-wide OpenRouter prompt-caching settings for Claude models."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, strict=True)
    anthropic_providers: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        default_factory=lambda: ["Amazon Bedrock"], max_length=10,
    )
    trim_slack_percent: int = Field(default=25, ge=0, le=90, strict=True)


class AssociativeMemoryConfig(BaseModel):
    """Framework-wide associative-memory settings; api_key names an environment variable, not a secret."""

    model_config = ConfigDict(extra="forbid")

    context_messages: int = Field(default=6, ge=0, le=100, strict=True)
    top_n: int = Field(default=3, ge=1, le=20, strict=True)
    score_threshold: float = Field(default=0.25, ge=0, le=1)
    query_model: Optional[str] = Field(default=None, strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)
    zdr: bool = Field(default=True, strict=True)


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


class PdfConfig(BaseModel):
    """PDF text extraction limits and Tesseract settings for OCR of pages without digital text."""

    model_config = ConfigDict(extra="forbid")

    max_pages: int = Field(default=30, ge=1, le=200, strict=True)
    ocr_min_chars: int = Field(default=25, ge=0, le=10000, strict=True)
    ocr_dpi: int = Field(default=300, ge=72, le=600, strict=True)
    ocr_languages: str = Field(default="eng+spa", min_length=1, max_length=64, pattern=r"^[A-Za-z_]+(\+[A-Za-z_]+)*$")
    tesseract_cmd: str | None = Field(default=None, min_length=1, max_length=512)


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
    # MCP servers from the shared catalog enabled for this agent, keyed by server id
    mcp: dict[McpServerId, AgentMcpConfig] = Field(default_factory=dict)
    # suggest relevant fragments from long-term memory each turn (true/false are accepted for llm/off)
    associative_memory: AssociativeMode = "llm"
    associative_execution: AssociativeExecution = "parallel"

    @field_validator("tool_groups")
    @classmethod
    def _validate_tool_groups(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else validate_tool_groups(value)


DreamToolGroup = Literal["web", "vault"]
# local wall-clock time of day, 24-hour HH:MM
_DAILY_AT = r"^(?:[01]\d|2[0-3]):[0-5]\d$"


class DreamConfig(BaseModel):
    """An agent's dream mode: schedule, model, limits and optional read tools of its background sessions."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, strict=True)  # runs the schedule; manual sessions work either way
    every_hours: Optional[int] = Field(default=None, ge=1, le=720, strict=True)
    daily_at: Optional[str] = Field(default=None, pattern=_DAILY_AT)
    model: Optional[str] = Field(default=None, strict=True, min_length=1, max_length=256)  # None = user's chat model
    reasoning_effort: Optional[ReasoningEffort] = "medium"  # None sends no reasoning request
    max_steps: int = Field(default=60, ge=1, le=500, strict=True)
    max_tokens: int = Field(default=500_000, ge=1_000, le=10_000_000, strict=True)  # input + output per session
    max_minutes: int = Field(default=5, ge=1, le=120, strict=True)  # wall-clock limit; then the session is force-closed
    tools: list[DreamToolGroup] = Field(default_factory=lambda: ["web", "vault"])
    prompt: str = Field(default="", max_length=20000)  # empty = built-in default
    notify_telegram: bool = Field(default=False, strict=True)

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("dream tool groups must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _one_schedule(self) -> "DreamConfig":
        if self.every_hours is not None and self.daily_at is not None:
            raise ValueError("set either dream.every_hours or dream.daily_at, not both")
        return self


class MemoryLimits(BaseModel):
    """Character limits of the memory files injected in every system prompt."""

    model_config = ConfigDict(extra="forbid")

    memory_chars: int = Field(default=2200, ge=100, le=50_000, strict=True)
    user_chars: int = Field(default=1400, ge=100, le=50_000, strict=True)


class AgentConfig(BaseModel):
    """One agent's own configuration; `id` identifies the agent and its data folder."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    chat_config: ChatConfig = ChatConfig()
    dream: DreamConfig = Field(default_factory=DreamConfig)
    vault: Optional[str] = Field(default=None, min_length=1, max_length=1024)  # folder exposed as the `vault` source
    memory_limits: MemoryLimits = Field(default_factory=MemoryLimits)


class ResolvedAgent(BaseModel):
    """Runtime dependencies resolved from the user's models/providers and agent behavior."""

    user_id: str
    language: str = "en"
    id: str
    name: str
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
    mcp: dict[str, AgentMcpConfig] = Field(default_factory=dict)
    associative_memory: AssociativeMode = "llm"
    associative_execution: AssociativeExecution = "parallel"
    dream: DreamConfig = Field(default_factory=DreamConfig)
    vault: Optional[str] = None
    memory_limits: MemoryLimits = Field(default_factory=MemoryLimits)
