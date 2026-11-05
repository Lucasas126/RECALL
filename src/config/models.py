from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, field_validator, model_validator

from src.agent.tool_groups import validate_tool_groups
from src.config.zdr import ensure_zdr_compatible
from src.config.model_refs import (
    CAPABILITIES, CLAUDE_SUBSCRIPTION, MAIN_PROVIDERS, OPENROUTER, prefix_of, validate_reference,
)
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
    zdr: Optional[bool] = Field(default=None, strict=True)  # None follows the user's require_zdr
    min_messages: int = Field(default=5, ge=1, le=1000, strict=True)

    @model_validator(mode="after")
    def _claude_subscription_needs_no_zdr(self) -> "ChatNamingConfig":
        _check_model_zdr(self.model, self.zdr, "chat_naming")
        return self


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
    score_threshold: float = Field(default=0.35, ge=0, le=1)
    query_model: Optional[str] = Field(default=None, strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)
    zdr: Optional[bool] = Field(default=None, strict=True)  # None follows the user's require_zdr

    @model_validator(mode="after")
    def _claude_subscription_needs_no_zdr(self) -> "AssociativeMemoryConfig":
        _check_model_zdr(self.query_model, self.zdr, "associative_memory")
        return self


class OrchestratorConfig(BaseModel):
    """Telegram orchestrator: a fast decision model picks the agent for a message that starts a new chat.

    api_key names an environment variable, not a secret. Opt-in: each user can also switch it on or off.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, strict=True)
    model: str = Field(default="typesafe/jev-1.14", strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)
    zdr: Optional[bool] = Field(default=None, strict=True)  # None follows the user's require_zdr
    min_confidence: float = Field(default=0.6, ge=0, le=1, allow_inf_nan=False)
    timeout_seconds: float = Field(default=4, ge=1, le=30, allow_inf_nan=False)
    grace_seconds: float = Field(default=8, ge=0, le=600, allow_inf_nan=False)
    uncertain_grace_seconds: float = Field(default=20, ge=0, le=600, allow_inf_nan=False)


class IndexConfig(BaseModel):
    """Framework-wide vector index settings: storage names, chunk sizes and the folder reconcile interval."""

    model_config = ConfigDict(extra="forbid")

    dir_name: str = Field(default=".index", min_length=1, max_length=64)
    collection: str = Field(default="memory", pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{1,62}[A-Za-z0-9]$")
    # files searched by the text tools; Markdown and text files are indexed, `.jsonl` only as conversations
    file_types: list[str] = Field(default_factory=lambda: ["txt", "md", "jsonl"], min_length=1)
    chunk_tokens: int = Field(default=400, ge=50, le=4000, strict=True)        # target chunk size (chars / 4)
    max_chunk_tokens: int = Field(default=600, ge=50, le=8000, strict=True)    # hard limit before a block is split
    overlap_tokens: int = Field(default=60, ge=0, le=1000, strict=True)        # repeated between chunks of a section
    embed_batch: int = Field(default=64, ge=1, le=2048, strict=True)           # texts per embeddings request
    folders_reconcile_minutes: float = Field(default=10, ge=0, le=10_080, allow_inf_nan=False)

    @model_validator(mode="after")
    def _sizes(self) -> "IndexConfig":
        if self.max_chunk_tokens < self.chunk_tokens:
            raise ValueError("index.max_chunk_tokens must be at least index.chunk_tokens")
        if self.overlap_tokens >= self.chunk_tokens:
            raise ValueError("index.overlap_tokens must be smaller than index.chunk_tokens")
        return self


class SpeechToTextConfig(BaseModel):
    """OpenRouter speech-to-text settings."""

    zdr: Optional[bool] = Field(default=None, strict=True)  # None follows the user's require_zdr
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


def _check_model_zdr(model: Optional[str], zdr: Optional[bool], section: str) -> None:
    """Validate the model reference; an explicit `zdr: true` cannot go with a Claude subscription model."""
    if model is not None:
        validate_reference(model)
    ensure_zdr_compatible(model, zdr, section)


class ProviderConfig(BaseModel):
    name: str
    api_key: Optional[str] = None  # name of the env var holding the actual key (not needed for local providers)

    @field_validator("name")
    @classmethod
    def _main_capable(cls, value: str) -> str:
        if value == CLAUDE_SUBSCRIPTION:
            raise ValueError(
                "claude_subscription cannot be the main provider (it has no embeddings or speech-to-text); keep "
                "OpenRouter or Ollama as `provider` and choose the model as models.chat: claude_subscription:<model>"
            )
        if value not in MAIN_PROVIDERS:
            raise ValueError(f"unknown provider {value!r}; use one of: {', '.join(MAIN_PROVIDERS)}")
        return value

    @field_validator("api_key")
    @classmethod
    def _env_name(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not re.match(_ENV_NAME, value):
            raise ValueError("api_key must be the name of an environment variable, not the key itself")
        return value


class OpenRouterConfig(BaseModel):
    """OpenRouter as an additional provider; when it is the main provider, `provider.api_key` is used."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)  # env var name, never the key


class OllamaConfig(BaseModel):
    """Ollama as a provider; `host` applies whether or not it is the main provider."""

    model_config = ConfigDict(extra="forbid")

    host: Optional[str] = Field(default=None, max_length=512, pattern=r"^https?://[^\s/?#]+(?:/[^\s?#]*)?$")  # None = OLLAMA_HOST or localhost


class ClaudeSubscriptionConfig(BaseModel):
    """Advanced options of the experimental Claude subscription provider (the user's own Claude Code login)."""

    model_config = ConfigDict(extra="forbid")

    claude_config_dir: Optional[str] = Field(default=None, min_length=1, max_length=1024)  # None = the CLI's default login
    command: Optional[str] = Field(default=None, min_length=1, max_length=1024)  # claude executable; None = PATH
    claude_code_telemetry: bool = Field(default=False, strict=True)  # False passes Claude Code's telemetry-off flags
    timeout_seconds: int = Field(default=300, ge=10, le=3600, strict=True)  # longest silence from Claude Code


class ClaudeLoginSettings(BaseModel):
    """Server-wide rule for the Claude Code CLI's DEFAULT login (the OS account running RECALL), from settings.yaml."""

    model_config = ConfigDict(extra="forbid")

    # ids of the users allowed to use the default login when they have no `claude_config_dir` of their own;
    # empty = nobody: every user must configure their own login folder
    default_login_users: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]] = Field(default_factory=list)


class ModelsConfig(BaseModel):
    """User-owned inference and indexing model defaults."""

    model_config = ConfigDict(extra="forbid")

    chat: str = Field(default="openai/gpt-4o-mini", strict=True, min_length=1, max_length=256)
    stt: str = Field(default="openai/whisper-large-v3-turbo", strict=True, min_length=1, max_length=256)
    embeddings: str = Field(default="openai/text-embedding-3-small", strict=True, min_length=1, max_length=256)

    @field_validator("chat")
    @classmethod
    def _valid_chat_reference(cls, value: str) -> str:
        return validate_reference(value)

    @field_validator("stt", "embeddings")
    @classmethod
    def _capable_provider(cls, value: str, info) -> str:
        validate_reference(value)
        capability = "stt" if info.field_name == "stt" else "embeddings"
        provider = prefix_of(value)
        if provider is not None and capability not in CAPABILITIES[provider]:
            raise ValueError(
                f"models.{info.field_name} cannot use {provider} (it has no {info.field_name}); "
                f"use a provider that serves {info.field_name}"
            )
        return value


_BACKEND_NAME = r"^[a-z][a-z0-9_-]{0,31}$"
TerminalApprovals = Literal["manual", "smart", "off"]


class SshBackendConfig(BaseModel):
    """A sandbox reached over SSH with the `ssh` binary; keys are file paths, never inline secrets."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["ssh"]
    host: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9.:_-]*$")
    port: int = Field(default=22, ge=1, le=65535, strict=True)
    user: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_.-]*$")
    key_file: str = Field(min_length=1, max_length=1024)
    known_hosts_file: str = Field(min_length=1, max_length=1024)
    description: str = Field(default="", max_length=200)  # free text shown to the model and in the agent form


class LocalBackendConfig(BaseModel):
    """Commands run on the RECALL host itself; refused unless settings.yaml `terminal.allow_local` is true."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["local"]
    workdir: str = Field(min_length=1, max_length=1024)
    description: str = Field(default="", max_length=200)


TerminalBackendConfig = Annotated[Union[SshBackendConfig, LocalBackendConfig], Field(discriminator="type")]


class AgentTerminalBackend(BaseModel):
    """How one agent uses one of the user's backends: its working directory and approval mode."""

    model_config = ConfigDict(extra="forbid")

    workdir: str = Field(default="~", min_length=1, max_length=1024)  # a path inside the backend
    approvals: TerminalApprovals = "manual"
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=600, strict=True)  # overrides the agent-wide one


class AgentTerminalConfig(BaseModel):
    """An agent's terminal tool group: which of the user's backends it may use, each with its own policy."""

    model_config = ConfigDict(extra="forbid")

    backends: dict[Annotated[str, StringConstraints(pattern=_BACKEND_NAME)], AgentTerminalBackend] = Field(min_length=1)
    default_backend: Optional[str] = Field(default=None, pattern=_BACKEND_NAME)  # used when a call names none
    timeout_seconds: int = Field(default=60, ge=1, le=600, strict=True)
    max_output_chars: int = Field(default=20_000, ge=500, le=200_000, strict=True)
    allow_in_dream: bool = Field(default=False, strict=True)  # unattended dream sessions may use the tools

    @model_validator(mode="after")
    def _default_is_allowed(self) -> "AgentTerminalConfig":
        if self.default_backend is None:
            if len(self.backends) > 1:
                raise ValueError("terminal.default_backend is required when more than one backend is allowed")
            self.default_backend = next(iter(self.backends))
        elif self.default_backend not in self.backends:
            raise ValueError(f"terminal.default_backend '{self.default_backend}' is not one of terminal.backends")
        return self

    def timeout_for_backend(self, name: str) -> int:
        return self.backends[name].timeout_seconds or self.timeout_seconds


class TerminalSettings(BaseModel):
    """Framework-wide terminal limits (settings.yaml `terminal:`)."""

    model_config = ConfigDict(extra="forbid")

    approval_timeout_seconds: int = Field(default=300, ge=5, le=86_400, strict=True)
    allow_local: bool = Field(default=False, strict=True)  # trusted single-user installs only
    default_timeout_seconds: int = Field(default=60, ge=1, le=3600, strict=True)
    max_timeout_seconds: int = Field(default=600, ge=1, le=3600, strict=True)
    max_output_chars: int = Field(default=20_000, ge=500, le=200_000, strict=True)
    # extra glob patterns (relative to the repository root, or absolute) the local backend may never touch
    protected_paths: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("protected_paths")
    @classmethod
    def _clean_protected_paths(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            if not isinstance(item, str) or not item.strip() or "\x00" in item or len(item) > 1024:
                raise ValueError("terminal.protected_paths entries must be non-empty strings without NUL bytes (max 1024 chars)")
            cleaned.append(item.strip())
        return cleaned

    @model_validator(mode="after")
    def _default_within_max(self) -> "TerminalSettings":
        if self.default_timeout_seconds > self.max_timeout_seconds:
            raise ValueError("terminal.default_timeout_seconds must not exceed max_timeout_seconds")
        return self


class SubagentSettings(BaseModel):
    """Framework-wide subagent limits (settings.yaml `subagents:`); an agent's `delegation` block can only lower them.

    Depth is fixed at 1: a subagent cannot launch tasks.
    """

    model_config = ConfigDict(extra="forbid")

    max_parallel_per_parent: int = Field(default=3, ge=1, le=10, strict=True)  # running tasks of one parent chat
    max_parallel_per_user: int = Field(default=4, ge=1, le=20, strict=True)  # running tasks of one user
    max_launches_per_turn: int = Field(default=5, ge=1, le=50, strict=True)  # task_start calls in one parent turn
    timeout_seconds: int = Field(default=600, ge=10, le=3600, strict=True)  # wall clock of one task
    max_tokens: int = Field(default=200_000, ge=1_000, le=5_000_000, strict=True)  # input + output tokens of one task
    result_max_chars: int = Field(default=8000, ge=500, le=100_000, strict=True)  # result text returned to the parent
    rate_limit_cooldown_seconds: int = Field(default=300, ge=60, le=86_400, strict=True)  # no launches after a 429
    max_followups: int = Field(default=5, ge=0, le=50, strict=True)  # task_send follow-ups per task
    max_auto_wakes: int = Field(default=3, ge=1, le=20, strict=True)  # wake-started parent turns in a row without a user message
    wake_debounce_seconds: float = Field(default=1.5, ge=0, le=30)  # results ending within this window share one wake


class AgentDelegationConfig(BaseModel):
    """An agent's `delegate` tool group: switch, grantable groups and limits (each can only lower the application's)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, strict=True)  # false switches the tools off without removing the group
    allowed_groups: list[str] = Field(default_factory=lambda: ["web"])  # groups the agent may grant a task (web only)
    max_parallel: Optional[int] = Field(default=None, ge=1, le=10, strict=True)  # per parent chat; None = application's
    timeout_seconds: Optional[int] = Field(default=None, ge=10, le=3600, strict=True)
    max_tokens: Optional[int] = Field(default=None, ge=1_000, le=5_000_000, strict=True)

    @field_validator("allowed_groups")
    @classmethod
    def _grantable(cls, value: list[str]) -> list[str]:
        from src.agent.tool_groups import DELEGATE_GRANTABLE_GROUPS

        if len(set(value)) != len(value):
            raise ValueError("delegation.allowed_groups must not contain duplicates")
        bad = sorted(set(value) - set(DELEGATE_GRANTABLE_GROUPS))
        if bad:
            raise ValueError(
                f"delegation.allowed_groups: {', '.join(bad)} cannot be granted to a task "
                f"(grantable: {', '.join(DELEGATE_GRANTABLE_GROUPS)})"
            )
        return value


class UserConfig(BaseModel):
    id: str
    name: str
    password_hash: Optional[str] = Field(default=None, min_length=1)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")
    # zero data retention: the default of every ZDR-aware feature (chats, pickers, naming, memory, dream, STT)
    require_zdr: bool = Field(default=False, strict=True)
    provider: ProviderConfig  # main provider: serves bare model ids
    embeddings_provider: Optional[ProviderConfig] = None  # falls back to `provider`
    agents: list[str]
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    claude_subscription: ClaudeSubscriptionConfig = Field(default_factory=ClaudeSubscriptionConfig)
    # sandboxes the user's agents may run commands in, by name (see docs/user-guide/terminal.md)
    terminal_backends: dict[Annotated[str, StringConstraints(pattern=_BACKEND_NAME)], TerminalBackendConfig] = Field(
        default_factory=dict,
    )

    def openrouter_settings(self) -> OpenRouterConfig:
        """OpenRouter settings with the key env var actually used (`provider.api_key` when it is main)."""
        if self.provider.name == OPENROUTER:
            return OpenRouterConfig(api_key=self.provider.api_key or self.openrouter.api_key)
        return self.openrouter


class ChatConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    stream: bool = True
    has_memory: bool = True
    reasoning: bool = True
    # effort level for models that support it (minimal|low|medium|high|xhigh|max); openrouter maps it
    reasoning_effort: Optional[str] = "medium"
    reasoning_tokens: Optional[int] = Field(default=None, ge=1)
    keep_reasoning_in_memory: bool = False
    # last N conversation messages kept in context (None = all); system prompt is always kept
    conversation_messages_retained: Optional[int] = Field(default=30, ge=1)
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
    # the agent's default chat model (`<provider>:<model>` or a bare id of the main provider); None follows the
    # user's default. A model chosen inside a chat still wins over it.
    model: Optional[str] = Field(default=None, strict=True, min_length=1, max_length=256)
    # the built-in, read-only `recall` skill (how RECALL works): listed in the prompt and readable with skill_view
    recall_knowledge: bool = True

    @field_validator("model")
    @classmethod
    def _valid_model_reference(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_reference(value)

    @field_validator("tool_groups")
    @classmethod
    def _validate_tool_groups(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else validate_tool_groups(value)


DreamToolGroup = Literal["web", "folders", "terminal"]
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
    tools: list[DreamToolGroup] = Field(default_factory=lambda: ["web", "folders"])
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
    """Application-wide character limits of SOUL.md and of the memory files injected in every system prompt."""

    model_config = ConfigDict(extra="forbid")

    soul_chars: int = Field(default=15_000, ge=1_000, le=100_000, strict=True)
    memory_chars: int = Field(default=2500, ge=100, le=50_000, strict=True)
    user_chars: int = Field(default=2500, ge=100, le=50_000, strict=True)


# source names the agent's own data already uses; a folder cannot take them
RESERVED_SOURCE_NAMES = frozenset({"mind_palace", "conversations", "agent", "dream"})
FOLDER_NAME_PATTERN = r"^[a-z0-9_-]{1,32}$"


class FolderConfig(BaseModel):
    """A user folder exposed to one agent as the source `<name>:`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=FOLDER_NAME_PATTERN)
    path: str = Field(min_length=1, max_length=1024)
    indexed: bool = Field(default=True, strict=True)     # used by associative memory and query_memory
    read_only: bool = Field(default=False, strict=True)  # never writable, whatever the tool groups

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, value: str) -> str:
        if value in RESERVED_SOURCE_NAMES:
            raise ValueError(f"folder name '{value}' is reserved; choose another (reserved: {', '.join(sorted(RESERVED_SOURCE_NAMES))})")
        return value


def _unique_folders(folders: list[FolderConfig]) -> list[FolderConfig]:
    names = [folder.name for folder in folders]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"folder names must be unique: {', '.join(duplicates)}")
    return folders


class AgentConfig(BaseModel):
    """One agent's own configuration; `id` identifies the agent and its data folder."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    chat_config: ChatConfig = ChatConfig()
    dream: DreamConfig = Field(default_factory=DreamConfig)
    folders: list[FolderConfig] = Field(default_factory=list, max_length=20)  # user folders, each a source
    terminal: Optional[AgentTerminalConfig] = None  # required by the `terminal` tool group
    delegation: Optional[AgentDelegationConfig] = None  # the `delegate` tool group; defaults when the group is selected

    @model_validator(mode="after")
    def _delegate_group_has_a_block(self) -> "AgentConfig":
        if "delegate" in (self.chat_config.tool_groups or ()) and self.delegation is None:
            self.delegation = AgentDelegationConfig()  # the web form cannot send the block: the defaults apply
        return self

    @model_validator(mode="after")
    def _dream_terminal_opt_in(self) -> "AgentConfig":
        if "terminal" in self.dream.tools and not (self.terminal and self.terminal.allow_in_dream):
            raise ValueError("dream.tools lists 'terminal': set terminal.allow_in_dream: true on this agent too")
        return self

    @model_validator(mode="before")
    @classmethod
    def _removed_vault(cls, data: object) -> object:
        if isinstance(data, dict) and "vault" in data:
            raise ValueError(
                "`vault` was replaced by `folders` (a list of name/path entries); "
                "move the path to `folders: [{name: vault, path: ...}]`"
            )
        # legacy: the size limits moved to settings.yaml (memory_limits); old agent files still carry the key
        if isinstance(data, dict) and "memory_limits" in data:
            data = {key: value for key, value in data.items() if key != "memory_limits"}
        return data

    @field_validator("folders")
    @classmethod
    def _validate_folders(cls, value: list[FolderConfig]) -> list[FolderConfig]:
        return _unique_folders(value)


class ResolvedAgent(BaseModel):
    """Runtime dependencies resolved from the user's models/providers and agent behavior."""

    user_id: str
    language: str = "en"
    require_zdr: bool = False
    id: str
    name: str
    models: ModelsConfig
    provider_name: str
    api_key: str
    embeddings_provider_name: str
    embeddings_api_key: str
    stream: bool = True
    has_memory: bool = True
    reasoning: bool = True
    reasoning_effort: Optional[str] = None
    reasoning_tokens: Optional[int] = Field(default=None, ge=1)
    keep_reasoning_in_memory: bool = False
    conversation_messages_retained: Optional[int] = None
    tools: bool = True
    tool_groups: list[str] | None = None
    allow: list[str] | None = None
    deny: list[str] | None = None
    mcp: dict[str, AgentMcpConfig] = Field(default_factory=dict)
    associative_memory: AssociativeMode = "llm"
    associative_execution: AssociativeExecution = "parallel"
    recall_knowledge: bool = True
    dream: DreamConfig = Field(default_factory=DreamConfig)
    folders: list[FolderConfig] = Field(default_factory=list)
    terminal: Optional[AgentTerminalConfig] = None
    delegation: Optional[AgentDelegationConfig] = None
    # the user's backends named by `terminal.backends` that resolved, by name; empty = the tools are not built
    terminal_backends: dict[str, TerminalBackendConfig] = Field(default_factory=dict)
    claude_subscription: ClaudeSubscriptionConfig = Field(default_factory=ClaudeSubscriptionConfig)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)  # key env var already resolved for a main OpenRouter
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
