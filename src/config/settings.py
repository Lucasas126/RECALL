from __future__ import annotations

from pathlib import Path

import yaml

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
