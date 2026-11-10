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


def audit_writable(path: Path | None) -> bool:
    """True when the audit file can be appended to (created if missing); no audit file configured counts as writable."""
    if path is None:
        return True
    try:
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8"):
                pass
    except OSError:
        logger.exception("the terminal audit file %s is not writable", path)
        return False
    return True


def write_audit(path: Path | None, **fields) -> bool:
    """Append one record; returns False when it could not be written (logged, never raised into the tool).

    The terminal session treats an unwritable audit file as a reason to refuse the action (fail closed).
    """
    if path is None:
        return True
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
        return False
    return True


def write_rule_audit(
    path: Path | None, *, actor: str, action: str, user: str, agent: str, rule: dict, index: int | None = None,
    **extra,
) -> bool:
    """Record a change of an agent's approval rules (`event: approval_rule`) in the same audit file.

    `action` is `add` or `remove`; `actor` says who did it (`user` from the web or a card, `admin` from the CLI).
    """
    if isinstance(rule.get("command"), str):
        rule = {**rule, "command": redact(rule["command"])[:_MAX_COMMAND]}
    fields = {"event": "approval_rule", "action": action, "actor": actor, "user": user, "agent": agent, "rule": rule}
    if index is not None:
        fields["index"] = index
    return write_audit(path, **fields, **extra)


def _redacted(value):
    """A copy of a JSON-like value with every `command` string redacted and shortened (rules hold commands)."""
    if isinstance(value, dict):
        return {key: (redact(item)[:_MAX_COMMAND] if key == "command" and isinstance(item, str) else _redacted(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redacted(item) for item in value]
    return value


def write_permission_audit(
    path: Path | None, *, actor: str, user: str, agent: str, target: str, tool: str, field: str, action: str,
    before: object, after: object, decision: str, **extra,
) -> bool:
    """Record an agent's permission change (`event: permission_change`), approved or not, in the same audit file.

    `actor` is the agent that asked, `target` the agent changed (`agent` is the owner of the file this line goes to),
    `decision` is `approved`, `denied`, `refused` (blocked before anyone was asked) or `unattended`.
    """
    fields = {
        "event": "permission_change", "actor": actor, "actor_kind": "agent", "user": user, "agent": agent,
        "target": target, "tool": tool, "field": field, "action": action,
        "before": _redacted(before), "after": _redacted(after), "decision": decision,
    }
    return write_audit(path, **fields, **{key: _redacted(value) for key, value in extra.items()})
