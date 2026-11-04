"""Per-agent audit file of terminal activity: one JSON line per executed or refused command, no output.

Location: `<data_dir>/<user>/<agent>/terminal_audit.jsonl`. The agent's own file tools (memory, palace, folders)
only reach their sources, and the sandbox tools only reach the sandbox, so none of them can read or edit it.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from src.terminal.output import redact
from src.utils.logger import get_logger

logger = get_logger("terminal")
AUDIT_FILE = "terminal_audit.jsonl"
_LOCK = threading.Lock()
_MAX_COMMAND = 2000


def audit_path(data_dir: Path, user_id: str, agent_id: str) -> Path:
    return Path(data_dir) / user_id / agent_id / AUDIT_FILE


def write_audit(path: Path | None, **fields) -> None:
    """Append one record; failures are logged, never raised into the tool."""
    if path is None:
        return
    record = {"ts": round(time.time(), 3), **fields}
    if isinstance(record.get("command"), str):
        record["command"] = redact(record["command"])[:_MAX_COMMAND]
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        logger.exception("could not write the terminal audit file %s", path)
