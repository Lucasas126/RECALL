"""The text behind the `permission_levels_help` tool: every level and what it allows, built from the code.

It reads `SERVICE_MIN_LEVEL` (levels.py) and `src.config.ceiling`, so it cannot drift from what is enforced. The prose of the
design page (docs/architecture/permissions.md) and the built-in manual say the same; `tests/test_permissions_tool_help.py`
compares them.
"""

from __future__ import annotations

from src.config import ceiling
from src.config.levels import SERVICE_MIN_LEVEL, Level, parse_level

LEVEL_TITLE = {
    Level.MINIMAL: "Minimal", Level.READER: "Reader", Level.ASSISTANT: "Assistant",
    Level.COLLABORATOR: "Collaborator", Level.OPERATOR: "Operator", Level.ADMIN: "Admin",
}

# what each service means, in one line (the key is a SERVICE_MIN_LEVEL key)
SERVICE_TEXT = {
    "memory_read": "read the agent's own memory (tool group memory_read)",
    "folders_read": "read the user's folders (tool group folders_read)",
    "memory_edit": "write the agent's memory (tool group memory_edit)",
    "self": "edit the agent's own MEMORY, USER, SOUL and skills (tool group self)",
    "web": "search the web (tool group web)",
    "folders_edit": "edit the user's folders (tool group folders_edit; asks)",
    "delegate": "launch web-only workers (tool group delegate; asks only where the rules say)",
    "mcp": "use MCP servers the user allows (asks)",
    "terminal_ssh": "use a terminal on an ssh backend without root (tool group terminal; always asks)",
    "delegation_targets": "hand tasks to the user's other agents (delegation.targets)",
    "permissions": "view and adjust the levels, tool groups and approval rules of the SAME user's other agents (tool group permissions; every change needs the user's approval)",
    "terminal_local": "use the `local` terminal backend (a shell on the RECALL host)",
    "terminal_root": "use an ssh backend whose user is root",
}


def _name(level: Level) -> str:
    return f"{level.label} ({int(level)})"


def needed_level(capability: str) -> Level | None:
    """The lowest level for a service key or a tool group id (`terminal` is the ssh class); None when unknown."""
    key = capability.strip().lower()
    if key in SERVICE_MIN_LEVEL:
        return SERVICE_MIN_LEVEL[key]
    if key in ceiling.GROUP_MIN_LEVEL:
        return ceiling.GROUP_MIN_LEVEL[key]
    return None


def capability_names() -> list[str]:
    """Everything `needed_level` knows: the services and the tool groups, sorted."""
    return sorted({*SERVICE_MIN_LEVEL, *ceiling.GROUP_MIN_LEVEL})


def levels_summary() -> list[dict]:
    """One entry per level: the tool groups it adds over the one below and the services whose minimum it is."""
    entries = []
    previous: set[str] = set()
    for level in Level:
        groups = set(ceiling.allowed_groups(level))
        entries.append({
            "level": level.label, "number": int(level),
            "adds_tool_groups": sorted(groups - previous),
            "services": _services_added(level),
            "tool_groups": sorted(groups),
        })
        previous = groups
    return entries


def levels_help(capability: str = "") -> str:
    """The help text: all levels and what each adds, or the level one capability needs."""
    asked = capability.strip()
    if asked:
        level = needed_level(asked)
        if level is None:
            return f"error: unknown capability '{asked[:60]}'; known: {', '.join(capability_names())}"
        text = SERVICE_TEXT.get(asked.lower(), f"the tool group {asked.lower()}")
        return f"{asked.lower()}: needs the level {_name(level)} or higher. {text}."
    lines = [
        "Permission levels, lowest to highest. A level is a ceiling over independent services: an agent's own configuration "
        "(tool groups, backends, MCP) can only narrow it. An agent's level is at most its owner's max_level, which only an "
        "administrator changes.",
        "",
    ]
    for entry in levels_summary():
        level = parse_level(entry["level"])
        added = [SERVICE_TEXT[key] for key in _services_added(level)]
        text = "; ".join(added) if added else "only the one or two tools chosen for the agent"
        if level == Level.ADMIN:
            text += "; act on other users' agents, data and settings, and the admin CLI (with approval)"
        lines.append(f"- {_name(level)} ({LEVEL_TITLE[level]}): {text}")
    lines += [
        "",
        "Rules that do not depend on the level:",
        "- Approvals are separate: layered rules (installation, user, agent) decide what asks, what is allowed and what is "
        "denied. Hardline commands and protected paths are always refused.",
        "- Allowing everything on a real machine (a blanket allow rule, default allow or approvals off) needs the level admin.",
        "- The `local` backend and root ssh users are admin only; unprivileged ssh backends start at operator.",
        "- Unattended runs (dreams, scheduled tasks) deny whatever would ask; they never change permissions.",
        "- An agent with the `permissions` group can only act on agents of its own user, never on itself, never above its own "
        "level or the user's max_level, and every change shows the user a card to approve once or deny.",
        "Recommend the lowest level that does the job.",
    ]
    return "\n".join(lines)


def _services_added(level: Level) -> list[str]:
    """The SERVICE_MIN_LEVEL keys whose minimum is exactly `level`, in table order."""
    if level == Level.MINIMAL:
        return []
    return [key for key, minimum in SERVICE_MIN_LEVEL.items() if minimum == level]
