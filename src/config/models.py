from __future__ import annotations

import re
from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from src.agent.tool_groups import validate_tool_groups
from src.config.levels import LevelField
from src.terminal.rules import ApprovalRuleList
from src.config.model_refs import (
    CAPABILITIES, CLAUDE_SUBSCRIPTION, MAIN_PROVIDERS, OPENROUTER, prefix_of, validate_reference,
)
ReasoningEffort = Literal["minimal", "low", "medium", "high", "xhigh", "max"]

_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"
# a bare file name inside the user's private MCP folder; no separators, so it cannot escape it
_STATE_FILE = r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$"
McpServerId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


# how an agent searches its memory each turn: not at all, with a query proposed by a small model, or with the user's message
AssociativeMode = Literal["off", "llm", "question_only"]
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


class McpTokenAuthConfig(BaseModel):
    """Each user pastes their own access token; it is sent as `Authorization: Bearer <token>`."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["token"]
    label: str = Field(default="Access token", min_length=1, max_length=100)
    help: str = Field(default="", max_length=500)  # where the user creates the token


class McpServerConfig(BaseModel):
    """One MCP server offered to every user; each user connects with their own credentials.

    `stdio` servers are a local command started per user; `http` servers are a remote
    streamable-HTTP endpoint reached with each user's own token.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    env: dict[str, McpEnvValue] = Field(default_factory=dict)
    url: str = ""
    exclude_tools: list[str] = Field(default_factory=list)
    auth: Optional[Union[McpTokenAuthConfig, McpAuthConfig]] = None

    @field_validator("env")
    @classmethod
    def _validate_env_names(cls, value: dict[str, McpEnvValue]) -> dict[str, McpEnvValue]:
        invalid = sorted(name for name in value if not re.match(_ENV_NAME, name))
        if invalid:
            raise ValueError(f"invalid environment variable name: {', '.join(invalid)}")
        return value

    @model_validator(mode="after")
    def _check_transport(self) -> "McpServerConfig":
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("a stdio MCP server needs a command")
            if self.url:
                raise ValueError("url is only for http MCP servers")
            if isinstance(self.auth, McpTokenAuthConfig):
                raise ValueError("token auth is only for http MCP servers")
        else:
            if self.command or self.args or self.env:
                raise ValueError("command, args and env are only for stdio MCP servers")
            if isinstance(self.auth, McpAuthConfig):
                raise ValueError("http MCP servers use auth: {type: token}")
            check_mcp_url(self.url)
        return self


_PRIVATE_HOST = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|\[::1\]|[a-z0-9-]+(\.(local|lan|internal|home\.arpa))?)$"
)


def check_mcp_url(url: str) -> str:
    """An http MCP server URL: https anywhere, or plain http only to a private-network host,
    so a user's token is never sent in clear text over the internet."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("an http MCP server needs an http(s) url")
    if parts.username or parts.password:
        raise ValueError("put credentials in the user's token, not in the url")
    host = parts.hostname.lower()
    host_for_match = f"[{host}]" if ":" in host else host
    if parts.scheme == "http" and not _PRIVATE_HOST.match(host_for_match):
        raise ValueError("plain http is only allowed to a private-network host; use https")
    return url


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

    model: str = Field(default="claude_subscription:haiku", strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    zdr: Optional[bool] = Field(default=None, strict=True)  # OpenRouter only; None follows the user's require_zdr
    min_messages: int = Field(default=5, ge=1, le=1000, strict=True)

    @field_validator("model")
    @classmethod
    def _valid_model(cls, value: str) -> str:
        validate_reference(value)
        return value


class ApprovalExplainConfig(BaseModel):
    """Framework-wide small model that explains an approval card in a sentence or two; api_key names an environment variable."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, strict=True)
    model: str = Field(default="claude_subscription:haiku", strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    zdr: Optional[bool] = Field(default=None, strict=True)  # OpenRouter only; None follows the user's require_zdr
    max_context_chars: int = Field(default=3000, ge=200, le=20_000, strict=True)

    @field_validator("model")
    @classmethod
    def _valid_model(cls, value: str) -> str:
        validate_reference(value)
        return value


class PromptCacheConfig(BaseModel):
    """Framework-wide OpenRouter prompt-caching settings for Claude models."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, strict=True)
    anthropic_providers: list[Annotated[str, StringConstraints(min_length=1, max_length=64)]] = Field(
        default_factory=lambda: ["Amazon Bedrock"], max_length=10,
    )


class AssociativeMemoryConfig(BaseModel):
    """Framework-wide associative-memory settings; api_key names an environment variable, not a secret."""

    model_config = ConfigDict(extra="forbid")

    context_messages: int = Field(default=8, ge=0, le=100, strict=True)
    top_n: int = Field(default=5, ge=1, le=20, strict=True)
    score_threshold: float = Field(default=0.45, ge=0, le=1)
    # of those, slots reserved for the agent's own mind palace; a palace chunk needs only `palace_threshold`
    palace_slots: int = Field(default=2, ge=0, le=20, strict=True)
    palace_threshold: float = Field(default=0.40, ge=0, le=1)
    # mind-palace path prefixes that do not count for the reserved slots (they still compete as normal hits)
    palace_exclude: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(default_factory=list, max_length=50)
    query_model: Optional[str] = Field(default="claude_subscription:haiku", strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)
    zdr: Optional[bool] = Field(default=None, strict=True)  # OpenRouter only; None follows the user's require_zdr

    @field_validator("query_model")
    @classmethod
    def _valid_query_model(cls, value: Optional[str]) -> Optional[str]:
        if value is not None:
            validate_reference(value)
        return value


class OrchestratorConfig(BaseModel):
    """Telegram orchestrator: a fast decision model picks the agent for a message that starts a new chat.

    api_key names an environment variable, not a secret. Opt-in: each user can also switch it on or off.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, strict=True)
    model: str = Field(default="typesafe/jev-1.14", strict=True, min_length=1, max_length=256)
    api_key: str = Field(default="OPENROUTER_API_KEY", pattern=_ENV_NAME)
    zdr: Optional[bool] = Field(default=None, strict=True)  # OpenRouter only; None follows the user's require_zdr
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
    """OpenRouter speech-to-text settings (instance defaults; the user's `models.stt_language` wins)."""

    zdr: Optional[bool] = Field(default=None, strict=True)  # OpenRouter only; None follows the user's require_zdr
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


class UploadsConfig(BaseModel):
    """Server-side copies of the files users attach to chats (settings.yaml `uploads`)."""

    model_config = ConfigDict(extra="forbid")

    retention_days: int = Field(default=30, ge=1, le=3650, strict=True)  # a copy older than this is removed, any file type


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

    @field_validator("command")
    @classmethod
    def _bare_command(cls, value: Optional[str]) -> Optional[str]:
        # a path would let user.yaml run any program on the server; absolute paths are the administrator's
        # (settings.yaml `claude_subscription.command` or the RECALL_CLAUDE_COMMAND environment variable)
        if value is not None and ("/" in value or "\\" in value or ":" in value):
            raise ValueError(
                "claude_subscription.command must be a bare executable name found on PATH (for example 'claude'), "
                "not a path; the administrator sets an absolute path in settings.yaml or RECALL_CLAUDE_COMMAND"
            )
        return value


class ClaudeLoginSettings(BaseModel):
    """Server-wide rule for the Claude Code CLI's DEFAULT login (the OS account running RECALL), from settings.yaml."""

    model_config = ConfigDict(extra="forbid")

    # ids of the users allowed to use the default login when they have no `claude_config_dir` of their own;
    # empty = nobody: every user must configure their own login folder
    default_login_users: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]] = Field(default_factory=list)
    # administrator-only absolute path of the claude executable, used when a user sets no `command` of their own
    command: Optional[str] = Field(default=None, min_length=1, max_length=1024)

    @field_validator("command")
    @classmethod
    def _absolute_command(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and not (value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)):
            raise ValueError("claude_subscription.command in settings.yaml must be an absolute path")
        return value


class ModelsConfig(BaseModel):
    """User-owned inference and indexing model defaults."""

    model_config = ConfigDict(extra="forbid")

    chat: str = Field(default="openai/gpt-4o-mini", strict=True, min_length=1, max_length=256)
    stt: str = Field(default="openai/whisper-large-v3-turbo", strict=True, min_length=1, max_length=256)
    embeddings: str = Field(default="openai/text-embedding-3-small", strict=True, min_length=1, max_length=256)
    # Spoken language for transcription (ISO-639-1, optionally with a region); None falls back to
    # `speech_to_text.language` in settings.yaml, then to the provider's auto-detection.
    stt_language: Optional[str] = Field(default=None, strict=True, pattern=r"^[a-z]{2}(?:-[A-Z]{2})?$")

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
SPACE_ID_PATTERN = r"^space-[0-9a-f]{8}$"
_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
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
    # `sandbox`: a disposable machine whose mistakes cost little; `machine`: one of the user's real machines. Approval
    # rules can match the kind, and a blanket allow on a machine is admin-only (docs/architecture/permissions.md).
    kind: Literal["sandbox", "machine"] = "machine"

    @property
    def backend_kind(self) -> str:
        return self.kind


class LocalBackendConfig(BaseModel):
    """Commands run on the RECALL host itself; refused unless settings.yaml `terminal.allow_local` is true."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["local"]
    workdir: str = Field(min_length=1, max_length=1024)
    description: str = Field(default="", max_length=200)

    @property
    def backend_kind(self) -> str:
        return "local"  # the RECALL host itself: always its own kind


TerminalBackendConfig = Annotated[Union[SshBackendConfig, LocalBackendConfig], Field(discriminator="type")]


class AgentTerminalBackend(BaseModel):
    """How one agent uses one of the user's backends: its working directory and approval mode."""

    model_config = ConfigDict(extra="forbid")

    workdir: str = Field(default="~", min_length=1, max_length=1024)  # a path inside the backend
    # shorthand scoped to this backend: the most specific default of the approval rules (see `shorthand`)
    approvals: TerminalApprovals = "manual"
    timeout_seconds: Optional[int] = Field(default=None, ge=1, le=600, strict=True)  # overrides the agent-wide one


class AgentTerminalConfig(BaseModel):
    """An agent's terminal tool group: which of the user's backends it may use, each with its own policy."""

    model_config = ConfigDict(extra="forbid")

    backends: dict[Annotated[str, StringConstraints(pattern=_BACKEND_NAME)], AgentTerminalBackend] = Field(min_length=1)
    default_backend: Optional[str] = Field(default=None, pattern=_BACKEND_NAME)  # used when a call names none
    timeout_seconds: int = Field(default=60, ge=1, le=600, strict=True)
    max_output_chars: int = Field(default=20_000, ge=500, le=200_000, strict=True)

    @model_validator(mode="before")
    @classmethod
    def _no_dream_terminal(cls, data: object) -> object:
        if isinstance(data, dict) and "allow_in_dream" in data:
            raise ValueError(
                "terminal.allow_in_dream was removed: dream sessions have no terminal; delete the key "
                "(read-only sandbox variants for dreams are planned)"
            )
        return data

    @model_validator(mode="after")
    def _default_is_allowed(self) -> "AgentTerminalConfig":
        if self.default_backend is None:
            if len(self.backends) > 1:
                raise ValueError("terminal.default_backend is required when more than one backend is allowed")
            self.default_backend = next(iter(self.backends))
        elif self.default_backend not in self.backends:
            raise ValueError(f"terminal.default_backend '{self.default_backend}' is not one of terminal.backends")
        return self

    def shorthand(self, name: str) -> Optional[TerminalApprovals]:
        """The `approvals` mode of a backend when the config sets it (a hand-made entry or a YAML key), else None.

        Absent means the approval rules' defaults decide; `manual` without the key and with it are the same thing
        when no rule list changes a default.
        """
        entry = self.backends[name]
        return entry.approvals if "approvals" in entry.model_fields_set else None

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

    max_parallel_per_parent: int = Field(default=5, ge=1, le=10, strict=True)  # running tasks of one parent chat
    max_parallel_per_user: int = Field(default=10, ge=1, le=20, strict=True)  # running tasks of one user
    max_launches_per_turn: int = Field(default=5, ge=1, le=50, strict=True)  # task_start calls in one parent turn
    timeout_seconds: int = Field(default=600, ge=10, le=3600, strict=True)  # wall clock of one task
    max_tokens: int = Field(default=0, ge=0, le=5_000_000, strict=True)  # input + output tokens of one task; 0 = no cap
    result_max_chars: int = Field(default=8000, ge=500, le=100_000, strict=True)  # result text returned to the parent
    rate_limit_cooldown_seconds: int = Field(default=300, ge=60, le=86_400, strict=True)  # no launches after a 429
    max_followups: int = Field(default=5, ge=0, le=50, strict=True)  # task_send follow-ups per task
    max_auto_wakes: int = Field(default=3, ge=1, le=20, strict=True)  # wake-started parent turns in a row without a user message
    wake_debounce_seconds: float = Field(default=1.5, ge=0, le=30)  # results ending within this window share one wake

    @field_validator("max_tokens")
    @classmethod
    def _cap_is_off_or_sensible(cls, value: int) -> int:
        if 0 < value < 1_000:
            raise ValueError("max_tokens must be 0 (no cap) or at least 1000")
        return value



class DelegationTarget(BaseModel):
    """An existing agent of the same user that `task_start(agent=...)` may hand a task to."""

    model_config = ConfigDict(extra="forbid")

    agent: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")  # id of one of the user's other agents
    remember: bool = Field(default=False, strict=True)  # true: the task chat is an ordinary hidden chat and the agent may write its OWN memory


class AgentDelegationConfig(BaseModel):
    """An agent's `delegate` tool group: switch, grantable groups and limits (each can only lower the application's)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, strict=True)  # false switches the tools off without removing the group
    targets: list[DelegationTarget] = Field(default_factory=list, max_length=20)  # existing agents it may delegate to; empty = ephemeral workers only
    allowed_groups: list[str] = Field(default_factory=lambda: ["web"])  # groups the agent may grant a task (web only)
    max_parallel: Optional[int] = Field(default=None, ge=1, le=10, strict=True)  # per parent chat; None = application's
    timeout_seconds: Optional[int] = Field(default=None, ge=10, le=3600, strict=True)
    max_tokens: Optional[int] = Field(default=None, ge=1_000, le=5_000_000, strict=True)
    # the model of fresh workers when task_start gets none (`<provider>:<model>` or a bare id of the main provider);
    # None = the model of the chat that launches the task
    default_model: Optional[str] = Field(default=None, strict=True, min_length=1, max_length=256)

    @field_validator("default_model")
    @classmethod
    def _valid_default_model(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_reference(value)

    @field_validator("targets")
    @classmethod
    def _unique_targets(cls, value: list[DelegationTarget]) -> list[DelegationTarget]:
        ids = [target.agent for target in value]
        if len(set(ids)) != len(ids):
            raise ValueError("delegation.targets must not list an agent twice")
        return value

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
    # highest level the user's agents may have; required, set only by an administrator (see config/levels.py).
    # A space declares none: it is derived from its members when the space is loaded (see config/spaces.py); until
    # then a space carries the lowest level, so an unresolved space never grants more than it should.
    max_level: LevelField
    # "user" is a person who logs in; "space" is a pseudo-user owning agents shared by `members`
    # (docs/project/design-shared-agents.md). A space has no password and never logs in.
    kind: Literal["user", "space"] = "user"
    members: list[str] = Field(default_factory=list)  # space only: the user ids sharing it (at least one)
    billing_user: Optional[str] = None  # space only: a member whose keys and Claude login the space uses
    # approval rules for all of the user's agents (the `user` layer; see docs/architecture/permissions.md)
    approval_rules: Optional[ApprovalRuleList] = None

    @model_validator(mode="before")
    @classmethod
    def _max_level_is_declared(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("kind") == "space":
            if data.get("max_level") is not None:
                raise ValueError(
                    f"space '{data.get('id', '?')}' must not declare max_level: it is the lowest max_level of its members"
                )
            return {**data, "max_level": "minimal"}  # placeholder, replaced by the loader (config/spaces.py)
        if isinstance(data, dict) and data.get("max_level") is None:
            raise ValueError(
                f"user '{data.get('id', '?')}' has no max_level (the highest permission level of its agents). "
                f"Set it with `python -m src.admin user set --user {data.get('id', '<user>')} --set max_level=<level>` "
                "(minimal, reader, assistant, collaborator, operator or admin)"
            )
        return data

    @model_validator(mode="after")
    def _kind_fields(self) -> "UserConfig":
        if self.kind == "user":
            if self.members or self.billing_user is not None:
                raise ValueError(f"user '{self.id}': members and billing_user belong to spaces (kind: space)")
            return self
        if not re.match(SPACE_ID_PATTERN, self.id):
            raise ValueError(f"space id '{self.id}' must look like space-<8 hex digits>")
        if self.password_hash:
            raise ValueError(f"space '{self.id}' must not have a password_hash: a space never logs in")
        if not self.members:
            raise ValueError(f"space '{self.id}' needs at least one member")
        if len(set(self.members)) != len(self.members):
            raise ValueError(f"space '{self.id}' lists a member twice")
        if any(not _USER_ID.match(member) or member == self.id for member in self.members):
            raise ValueError(f"space '{self.id}': members must be user ids")
        if self.billing_user is None or self.billing_user not in self.members:
            raise ValueError(f"space '{self.id}': billing_user is required and must be one of its members")
        return self

    @property
    def claude_owner(self) -> str:
        """Whose config folder holds the Claude login: the user, or a space's `billing_user` (never another path)."""
        return self.billing_user if self.kind == "space" and self.billing_user else self.id

    def openrouter_settings(self) -> OpenRouterConfig:
        """OpenRouter settings with the key env var actually used (`provider.api_key` when it is main)."""
        if self.provider.name == OPENROUTER:
            return OpenRouterConfig(api_key=self.provider.api_key or self.openrouter.api_key)
        return self.openrouter


# upper bound of the per-agent tool round-trip override
MAX_TOOL_HOPS_LIMIT = 100


class CompactionConfig(BaseModel):
    """An agent's context compaction: when the context passes `trigger`, a small model summarises the older part.

    `mode` picks the unit of `trigger` and `keep_recent`: `percent` of the chat model's context window, or `tokens`.
    The system prompt is always kept whole; `keep_recent` is the recent tail kept verbatim.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, strict=True)
    mode: Literal["percent", "tokens"] = "percent"
    trigger: int = Field(default=50, ge=1, le=10_000_000, strict=True)
    keep_recent: int = Field(default=20, ge=0, le=10_000_000, strict=True)
    # the summary model (`<provider>:<model>` or a bare id of the main provider); None = the chat's own model
    model: Optional[str] = Field(default="claude_subscription:haiku", strict=True, min_length=1, max_length=256)

    @field_validator("model")
    @classmethod
    def _valid_model_reference(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_reference(value)

    @model_validator(mode="after")
    def _sane_bounds(self) -> "CompactionConfig":
        if self.mode == "percent" and (self.trigger > 95 or self.keep_recent > 90):
            raise ValueError("compaction in percent mode: trigger must be at most 95 and keep_recent at most 90")
        if self.mode == "tokens" and self.trigger < 1_000:
            raise ValueError("compaction in tokens mode: trigger must be at least 1000 tokens")
        if self.keep_recent >= self.trigger:
            raise ValueError("compaction.keep_recent must be smaller than compaction.trigger")
        return self


class ChatConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    stream: bool = True
    has_memory: bool = True
    reasoning: bool = True
    # effort level for models that support it (minimal|low|medium|high|xhigh|max); openrouter maps it
    reasoning_effort: Optional[str] = "medium"
    reasoning_tokens: Optional[int] = Field(default=None, ge=1)
    keep_reasoning_in_memory: bool = True
    # tool round-trips per turn for this agent (None = `agent.max_tool_hops` from settings.yaml)
    max_tool_hops: Optional[int] = Field(default=None, ge=1, le=MAX_TOOL_HOPS_LIMIT)
    # summarise the older part of a long chat with a small model (see CompactionConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
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
    max_tokens: int = Field(default=500_000, ge=0, le=100_000_000, strict=True)  # input + output per session; 0 = no cap
    max_minutes: int = Field(default=5, ge=1, le=120, strict=True)  # wall-clock limit; then the session is force-closed
    tools: list[DreamToolGroup] = Field(default_factory=lambda: ["folders"])
    prompt: str = Field(default="", max_length=20000)  # empty = built-in default
    notify_telegram: bool = Field(default=False, strict=True)
    # read-only workers that read chats for the dream (also needs the `delegate` group and level collaborator)
    delegate: bool = Field(default=False, strict=True)
    # model of those workers (e.g. a cheaper one); None = the dream's model
    delegate_model: Optional[str] = Field(default=None, strict=True, min_length=1, max_length=256)

    @field_validator("delegate_model")
    @classmethod
    def _valid_delegate_model(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_reference(value)

    @field_validator("max_tokens")
    @classmethod
    def _token_cap_is_off_or_sensible(cls, value: int) -> int:
        if 0 < value < 1_000:
            raise ValueError("dream.max_tokens must be 0 (no cap) or at least 1000")
        return value

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: list[str]) -> list[str]:
        if "terminal" in value:
            raise ValueError("dream.tools: 'terminal' is not allowed: dream sessions have no terminal (use web or folders)")
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
RESERVED_SOURCE_NAMES = frozenset({"mind_palace", "conversations", "agent", "dream", "uploads"})
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
    # permission level, at most the owner's max_level; None = not declared (the lowest level covering the config applies)
    level: Optional[LevelField] = None
    approval_rules: Optional[ApprovalRuleList] = None  # the `agent` layer of the approval rules
    # profile picture style: `circle` crops it into the accent-bordered circle, `free` shows it as it is (transparent PNGs)
    avatar_style: Literal["circle", "free"] = "circle"
    # UI-level only: a hidden agent is left out of the web agent pickers; @mention, delegation and chats work as for any agent
    hidden: bool = False

    @model_validator(mode="after")
    def _delegate_group_has_a_block(self) -> "AgentConfig":
        if "delegate" in (self.chat_config.tool_groups or ()) and self.delegation is None:
            self.delegation = AgentDelegationConfig()  # a config that selects the group without a block gets the defaults
        return self

    @model_validator(mode="after")
    def _no_self_target(self) -> "AgentConfig":
        if self.delegation is not None and any(target.agent == self.id for target in self.delegation.targets):
            raise ValueError("delegation.targets must not list the agent itself")
        return self

    @model_validator(mode="before")
    @classmethod
    def _removed_vault(cls, data: object) -> object:
        if isinstance(data, dict) and "vault" in data:
            raise ValueError(
                "`vault` was replaced by `folders` (a list of name/path entries); "
                "move the path to `folders: [{name: vault, path: ...}]`"
            )
        # the API view of an agent carries a read-only `avatar` (url, version, style); a body sent back as read is accepted
        if isinstance(data, dict) and "avatar" in data:
            data = {key: value for key, value in data.items() if key != "avatar"}
        return data

    @field_validator("folders")
    @classmethod
    def _validate_folders(cls, value: list[FolderConfig]) -> list[FolderConfig]:
        return _unique_folders(value)


class ResolvedAgent(BaseModel):
    """Runtime dependencies resolved from the user's models/providers and agent behavior."""

    user_id: str
    # space-owned agent only: member id -> display name, and the member who is the speaker when none is known
    space_members: dict[str, str] = Field(default_factory=dict)
    space_billing_user: Optional[str] = None
    claude_user_id: Optional[str] = None  # whose config folder holds the Claude login; None = `user_id` (spaces: billing user)
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
    keep_reasoning_in_memory: bool = True
    max_tool_hops: Optional[int] = Field(default=None, ge=1, le=MAX_TOOL_HOPS_LIMIT)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
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
    # effective permission levels, resolved at load. A hand-built config without `level` gets the lowest level that
    # covers what it is configured with; `max_level` stays None then.
    level: Optional[LevelField] = None
    max_level: Optional[LevelField] = None
    # the user and agent layers of the approval rules (the installation layer is settings.APPROVAL_RULES)
    user_approval_rules: Optional[ApprovalRuleList] = None
    approval_rules: Optional[ApprovalRuleList] = None

    @model_validator(mode="after")
    def _effective_level(self) -> "ResolvedAgent":
        if self.level is None:
            from src.config.levels import required_level

            self.level = required_level(self)
        return self
