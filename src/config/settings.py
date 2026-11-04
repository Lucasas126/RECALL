from __future__ import annotations

import os
from pathlib import Path

import yaml

from src.config.models import (
    AssociativeMemoryConfig, ChatNamingConfig, ClaudeLoginSettings, IndexConfig, McpSettings, PdfConfig, PromptCacheConfig, SpeechToTextConfig, TavilyConfig,
    VideoDecompositionConfig,
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

# ------------------------------------------------------------------ vector index
INDEX = IndexConfig.model_validate(_raw["index"])
INDEX_DIR_NAME = INDEX.dir_name                  # index folder inside each agent's data dir
INDEX_COLLECTION = INDEX.collection              # chroma collection name
INDEX_FILE_TYPES = set(INDEX.file_types)         # files searched by the text tools; md/txt are also indexed

# ------------------------------------------------------------------ agent
AGENT_MAX_TOOL_HOPS = _agent["max_tool_hops"]    # max tool round-trips per turn

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

# ------------------------------------------------------------------ speech to text
SPEECH_TO_TEXT = SpeechToTextConfig.model_validate(_speech_to_text)

# ------------------------------------------------------------------ video decomposition
VIDEO_DECOMPOSITION = VideoDecompositionConfig.model_validate(_video)

# ------------------------------------------------------------------ pdf text extraction
PDF = PdfConfig.model_validate(_pdf)

# ------------------------------------------------------------------ prompt caching
PROMPT_CACHE = PromptCacheConfig.model_validate(_raw.get("prompt_cache", {}))

# ------------------------------------------------------------------ external web access
TAVILY = TavilyConfig.model_validate(_raw.get("tavily", {}))

# ------------------------------------------------------------------ chat naming
CHAT_NAMING = ChatNamingConfig.model_validate(_raw.get("chat_naming", {}))

# ------------------------------------------------------------------ mcp servers
MCP = McpSettings.model_validate(_raw.get("mcp", {}))

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
