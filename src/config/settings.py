from __future__ import annotations

from pathlib import Path

import yaml

from src.config.models import ChatNamingConfig, SpeechToTextConfig, TavilyConfig, VideoDecompositionConfig

"""Framework-level settings, loaded from `settings.yaml` at the repo root.

This is the RECALL system's own configuration, distinct from the per-user
and per-agent configuration authored as YAML under `CONFIG_DIR`.
"""

_SETTINGS_FILE = Path(__file__).resolve().parents[2] / "settings.yaml"

with open(_SETTINGS_FILE, "r", encoding="utf-8") as _fh:
    _raw = yaml.safe_load(_fh) or {}

_paths = _raw["paths"]
_index = _raw["index"]
_agent = _raw["agent"]
_assoc = _raw["associative_memory"]
_web = _raw["web"]
_telegram = _raw["telegram"]
_speech_to_text = _raw["speech_to_text"]
_video = _raw["video"]

# ------------------------------------------------------------------ paths
CONFIG_DIR = Path(_paths["config_dir"])  # user & agent yaml configs
DATA_DIR = Path(_paths["data_dir"])      # per-agent data plane (sources + index)
LOGS_DIR = Path(_paths["logs_dir"])      # per-run log folders

# ------------------------------------------------------------------ vector index
INDEX_DIR_NAME = _index["dir_name"]              # index folder inside each agent's data dir
INDEX_COLLECTION = _index["collection"]          # chroma collection name
INDEX_FILE_TYPES = set(_index["file_types"])     # source files eligible for indexing
INDEX_CHUNK_WORDS = _index["chunk_words"]        # words per chunk for non-jsonl files

# ------------------------------------------------------------------ agent
AGENT_MAX_TOOL_HOPS = _agent["max_tool_hops"]    # max tool round-trips per turn

# ------------------------------------------------------------------ web
WEB_HOST = _web["host"]                          # interface the web ui binds to
WEB_PORT = _web["port"]                          # port the web ui listens on

# ------------------------------------------------------------------ telegram
TELEGRAM_ALLOWED_USERS_ENV = _telegram["allowed_users_env"]

# ------------------------------------------------------------------ speech to text
SPEECH_TO_TEXT = SpeechToTextConfig.model_validate(_speech_to_text)

# ------------------------------------------------------------------ video decomposition
VIDEO_DECOMPOSITION = VideoDecompositionConfig.model_validate(_video)

# ------------------------------------------------------------------ external web access
TAVILY = TavilyConfig.model_validate(_raw.get("tavily", {}))

# ------------------------------------------------------------------ chat naming
CHAT_NAMING = ChatNamingConfig.model_validate(_raw.get("chat_naming", {}))

# ------------------------------------------------------------------ associative memory
ASSOC_CONTEXT_MESSAGES = _assoc["context_messages"]   # recent messages the memory subagent sees
ASSOC_TOP_N = _assoc["top_n"]                        # fragments injected per turn
ASSOC_SCORE_THRESHOLD = _assoc["score_threshold"]    # min similarity to inject a fragment
