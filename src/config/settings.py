from __future__ import annotations

import os
import threading
from pathlib import Path

import yaml

from src.config.models import (
    ApprovalExplainConfig, AssociativeMemoryConfig, ChatNamingConfig, ClaudeLoginSettings, IndexConfig, McpSettings, MemoryLimits, OrchestratorConfig, PdfConfig, PromptCacheConfig, SpeechToTextConfig, TavilyConfig, TerminalSettings, UploadsConfig,
    SubagentSettings, VideoDecompositionConfig,
)

"""Framework-level settings, loaded from `settings.yaml` at the repo root.

This is the RECALL system's own configuration, distinct from the per-user
and per-agent configuration authored as YAML under `CONFIG_DIR`.
"""

_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "settings.yaml"

with open(_SETTINGS_FILE, "r", encoding="utf-8") as _fh:
    _raw = yaml.safe_load(_fh) or {}

_paths = _raw["paths"]
_agent = _raw["agent"]
_web = _raw["web"]
_telegram = _raw["telegram"]
_speech_to_text = _raw["speech_to_text"]
_video = _raw["video"]
_pdf = _raw["pdf"]

# ------------------------------------------------------------------ paths
CONFIG_DIR = Path(_paths["config_dir"])  # user & agent yaml configs
DATA_DIR = Path(_paths["data_dir"])      # per-agent data plane (sources + index)
LOGS_DIR = Path(_paths["logs_dir"])      # per-run log folders

# ------------------------------------------------------------------ user folders
_folders = _raw.get("folders") or {}
FOLDERS_ALLOWED_ROOTS: list[str] = list(_folders.get("allowed_roots") or [])  # empty: any path outside the reserved ones
if not all(isinstance(_item, str) and _item.strip() and "\x00" not in _item for _item in FOLDERS_ALLOWED_ROOTS):
    raise ValueError("folders.allowed_roots must be a list of non-empty path strings")

# ------------------------------------------------------------------ vector index
INDEX = IndexConfig.model_validate(_raw["index"])
INDEX_DIR_NAME = INDEX.dir_name                  # index folder inside each agent's data dir
INDEX_COLLECTION = INDEX.collection              # chroma collection name
INDEX_FILE_TYPES = set(INDEX.file_types)         # files searched by the text tools; md/txt are also indexed

# ------------------------------------------------------------------ agent
AGENT_MAX_TOOL_HOPS = _agent["max_tool_hops"]    # max tool round-trips per turn

# ------------------------------------------------------------------ memory size limits
# application-wide maximum sizes of SOUL.md, MEMORY.md and USER.md (not configurable per user or agent)
MEMORY_LIMITS = MemoryLimits.model_validate(_raw.get("memory_limits") or {})


def memory_limits() -> MemoryLimits:
    """The configured character limits of SOUL.md, MEMORY.md and USER.md (read from settings.yaml at start-up)."""
    return MEMORY_LIMITS


# ------------------------------------------------------------------ web
# interface the web ui binds to; RECALL_WEB_HOST overrides it (the Docker image sets 0.0.0.0)
WEB_HOST = os.environ.get("RECALL_WEB_HOST") or _web["host"]
WEB_PORT = _web["port"]                          # port the web ui listens on
WEB_SESSION_SECRET_ENV = _web["session_secret_env"]


def _public_url(value: str | None) -> str:
    """The web ui's public base address without a trailing slash; empty when not configured."""
    url = (value or "").strip().rstrip("/")
    if url and not url.lower().startswith(("http://", "https://")):
        raise ValueError(f"web.public_url / RECALL_PUBLIC_URL must start with http:// or https://, got {url!r}")
    return url


# base address of the web ui as users open it (e.g. http://<server-ip>:8000), used for links in Telegram
# dream notifications; empty = text-only notifications. RECALL_PUBLIC_URL overrides it.
WEB_PUBLIC_URL = _public_url(os.environ.get("RECALL_PUBLIC_URL") or _web.get("public_url"))

# ------------------------------------------------------------------ telegram
TELEGRAM_ALLOWED_USERS_ENV = _telegram["allowed_users_env"]


def parse_chat_idle_minutes(yaml_value: object, env_value: str | None = None) -> float:
    """Minutes without a message after which Telegram starts a new chat.

    `telegram.chat_idle_minutes` (default 60, integer 1..10080) is overridden by the local environment variable
    RECALL_TELEGRAM_CHAT_IDLE_MINUTES (any positive number, fractional allowed, e.g. 0.5 for tests).
    """
    if env_value is not None and env_value.strip():
        try:
            minutes = float(env_value)
        except ValueError:
            raise ValueError(f"RECALL_TELEGRAM_CHAT_IDLE_MINUTES must be a positive number, got {env_value!r}") from None
        if not 0 < minutes < float("inf"):
            raise ValueError(f"RECALL_TELEGRAM_CHAT_IDLE_MINUTES must be a positive number, got {env_value!r}")
        return minutes
    if yaml_value is None:
        return 60.0
    if isinstance(yaml_value, bool) or not isinstance(yaml_value, int) or not 1 <= yaml_value <= 10080:
        raise ValueError(f"telegram.chat_idle_minutes must be an integer from 1 to 10080, got {yaml_value!r}")
    return float(yaml_value)


# minutes of silence after which a Telegram message starts a new chat
TELEGRAM_CHAT_IDLE_MINUTES = parse_chat_idle_minutes(
    _telegram.get("chat_idle_minutes"), os.environ.get("RECALL_TELEGRAM_CHAT_IDLE_MINUTES"),
)

# ------------------------------------------------------------------ speech to text
SPEECH_TO_TEXT = SpeechToTextConfig.model_validate(_speech_to_text)

# ------------------------------------------------------------------ video decomposition
VIDEO_DECOMPOSITION = VideoDecompositionConfig.model_validate(_video)

# ------------------------------------------------------------------ pdf text extraction
PDF = PdfConfig.model_validate(_pdf)

# ------------------------------------------------------------------ uploaded attachments kept on disk
UPLOADS = UploadsConfig.model_validate(_raw.get("uploads") or {})

# ------------------------------------------------------------------ prompt caching
PROMPT_CACHE = PromptCacheConfig.model_validate(_raw.get("prompt_cache", {}))

# ------------------------------------------------------------------ external web access
TAVILY = TavilyConfig.model_validate(_raw.get("tavily", {}))

# ------------------------------------------------------------------ chat naming
CHAT_NAMING = ChatNamingConfig.model_validate(_raw.get("chat_naming", {}))

# ------------------------------------------------------------------ approval explanations
APPROVAL_EXPLAIN = ApprovalExplainConfig.model_validate(_raw.get("approval_explain", {}))

# ------------------------------------------------------------------ mcp servers
# The installation can add servers without editing the tracked settings.yaml: CONFIG_DIR/mcp_servers.yaml
# holds a `servers:` map in the same format. It keeps local URLs out of the repo and survives image updates.
MCP_LOCAL_FILE = CONFIG_DIR / "mcp_servers.yaml"


def _mcp_settings(raw: dict, local_file: Path) -> McpSettings:
    """settings.yaml `mcp`, plus the servers of the optional local catalog file."""
    mcp_raw = dict(raw.get("mcp") or {})
    if local_file.is_file():
        with open(local_file, "r", encoding="utf-8") as fh:
            local = yaml.safe_load(fh) or {}
        if not isinstance(local, dict) or set(local) - {"servers"}:
            raise ValueError(f"{local_file}: only a `servers:` map is allowed")
        extra = local.get("servers") or {}
        if not isinstance(extra, dict):
            raise ValueError(f"{local_file}: `servers` must be a map of server id to server")
        shared = set(extra) & set(mcp_raw.get("servers") or {})
        if shared:
            raise ValueError(f"{local_file}: server ids already in settings.yaml: {', '.join(sorted(shared))}")
        mcp_raw["servers"] = {**(mcp_raw.get("servers") or {}), **extra}
    return McpSettings.model_validate(mcp_raw)


MCP = _mcp_settings(_raw, MCP_LOCAL_FILE)  # catalog at import time; `current_mcp_settings()` follows the file


class _McpCatalog:
    """The MCP catalog, re-read when `mcp_servers.yaml` appears, disappears or changes."""

    def __init__(self, raw: dict, local_file: Path, initial: McpSettings) -> None:
        self._raw = raw
        self._file = local_file
        self._settings = initial
        self._stamp = self._stat()
        self._lock = threading.Lock()

    def _stat(self) -> tuple[int, int] | None:
        try:
            info = self._file.stat()
        except OSError:
            return None
        return info.st_mtime_ns, info.st_size

    def current(self) -> McpSettings:
        with self._lock:
            stamp = self._stat()
            if stamp != self._stamp:
                self._stamp = stamp  # an invalid file is reported once per change, not on every call
                try:
                    self._settings = _mcp_settings(self._raw, self._file)
                except (OSError, ValueError, yaml.YAMLError) as exc:  # pydantic's ValidationError is a ValueError
                    from src.utils.logger import get_logger

                    get_logger("config").error("%s is invalid, keeping the previous MCP catalog: %s", self._file, exc)
            return self._settings


_MCP_CATALOG = _McpCatalog(_raw, MCP_LOCAL_FILE, MCP)


def current_mcp_settings() -> McpSettings:
    """The MCP settings now: `MCP` plus the latest valid `mcp_servers.yaml` (a broken edit keeps the last good one)."""
    return _MCP_CATALOG.current()

# ------------------------------------------------------------------ claude subscription: who may use the default login
# RECALL_CLAUDE_DEFAULT_LOGIN_USERS (comma-separated user ids), when set, replaces the yaml list: a local override
# that keeps personal user ids out of the tracked settings.yaml.
_claude = dict(_raw.get("claude_subscription") or {})
_claude_env = os.environ.get("RECALL_CLAUDE_DEFAULT_LOGIN_USERS")
if _claude_env is not None and _claude_env.strip():
    _claude["default_login_users"] = [part.strip() for part in _claude_env.split(",") if part.strip()]
CLAUDE_LOGIN = ClaudeLoginSettings.model_validate(_claude)

# ------------------------------------------------------------------ associative memory
ASSOC = AssociativeMemoryConfig.model_validate(_raw.get("associative_memory", {}))

# ------------------------------------------------------------------ telegram orchestrator
ORCHESTRATOR = OrchestratorConfig.model_validate(_raw.get("orchestrator") or {})

# ------------------------------------------------------------------ subagents (the `delegate` tool group)
SUBAGENTS = SubagentSettings.model_validate(_raw.get("subagents") or {})

# ------------------------------------------------------------------ terminal sandbox
def parse_allow_local(yaml_value: object, env_value: str | None = None) -> object:
    """`terminal.allow_local`, overridden by RECALL_TERMINAL_ALLOW_LOCAL (true/false, 1/0, yes/no, on/off)."""
    if env_value is not None and env_value.strip():
        word = env_value.strip().lower()
        if word in ("true", "1", "yes", "on"):
            return True
        if word in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"RECALL_TERMINAL_ALLOW_LOCAL must be true or false, got {env_value!r}")
    return False if yaml_value is None else yaml_value  # a non-boolean yaml value is rejected by TerminalSettings


_terminal = dict(_raw.get("terminal") or {})
_terminal["allow_local"] = parse_allow_local(_terminal.get("allow_local"), os.environ.get("RECALL_TERMINAL_ALLOW_LOCAL"))
TERMINAL = TerminalSettings.model_validate(_terminal)


# ------------------------------------------------------------------ approval rules (the installation layer)
from src.terminal.rules import ApprovalRuleList  # noqa: E402  (after the models: it validates the settings block)

APPROVAL_RULES = ApprovalRuleList.model_validate(_raw.get("approval_rules") or {})
