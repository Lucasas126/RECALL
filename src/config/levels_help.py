"""The text behind the `permission_levels_help` tool: every level and what it allows, built from the code.

It reads `SERVICE_MIN_LEVEL` (levels.py) and `src.config.ceiling`, so it cannot drift from what is enforced. The prose of the
design page (docs/architecture/permissions.md) and the built-in manual say the same; `tests/test_permissions_tool_help.py`
compares them.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.config import ceiling
from src.config.levels import SERVICE_MIN_LEVEL, Level, backend_min_level, parse_level

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
    "notify": "send the user a short Telegram message, at most 30 an hour (tool group notify)",
    "files_send": "send the user a file the agent can already read (tool group files_send)",
    "identity": "change its own display name, nothing else (tool group identity)",
    "automations": "schedule things for later, at most 50 per user (tool group automations)",
    "folders_edit": "edit the user's folders (tool group folders_edit; asks)",
    "delegate": "launch web-only workers (tool group delegate; asks only where the rules say)",
    "mcp": "use MCP servers the user allows (asks)",
    "terminal_ssh": "use a terminal on an ssh backend without root (tool group terminal; always asks)",
    "delegation_targets": "hand tasks to the user's other agents (delegation.targets)",
    "permissions": "view and adjust the levels, tool groups and approval rules of the SAME user's agents, itself included within its own level (tool group permissions; every change needs the user's approval; its own level only lower)",
    "secrets": "list the NAMES of the stored secrets and copy, rename or delete them without ever seeing a value (tool group secrets; every change needs the user's approval; admin only)",
    "agents": "list, create, edit, rename and delete the SAME user's agents, never their level, tool groups or approval rules (tool group agents; every change needs the user's approval)",
    "terminal_local": "use the `local` terminal backend (a shell on the RECALL host)",
    "terminal_root": "use an ssh backend whose user is root",
}


MINIMAL_TEXT = "only the one or two tools chosen for the agent"
ADMIN_EXTRA_TEXT = "act on other users' agents, data and settings, and the admin CLI (with approval)"


def level_description_parts(level: Level) -> list[str]:
    """What `level` adds over the one below, as the texts of its services (the web selector and the help share them)."""
    parts = [SERVICE_TEXT[key] for key in _services_added(level)] or [MINIMAL_TEXT]
    if level == Level.ADMIN:
        parts.append(ADMIN_EXTRA_TEXT)
    return parts


def levels_catalog() -> dict:
    """Everything a client needs to show levels and compute the level a configuration requires (GET /api/levels).

    Built from `SERVICE_MIN_LEVEL`, `ceiling.GROUP_MIN_LEVEL` and `backend_min_level`, so it cannot drift from what is
    enforced. The user's own `max_level` is added by the service.
    """
    classes = {
        "local": SimpleNamespace(type="local"),
        "ssh_root": SimpleNamespace(type="ssh", user="root"),
        "ssh": SimpleNamespace(type="ssh", user=""),
    }
    return {
        "levels": [
            {
                "name": level.label, "number": int(level), "title": LEVEL_TITLE[level],
                "parts": level_description_parts(level), "description": "; ".join(level_description_parts(level)),
                "tool_groups": sorted(ceiling.allowed_groups(level)),
            }
            for level in Level
        ],
        "groups": {group: level.label for group, level in ceiling.GROUP_MIN_LEVEL.items()},
        "services": {key: level.label for key, level in SERVICE_MIN_LEVEL.items()},
        "backend_rules": {name: backend_min_level(backend).label for name, backend in classes.items()},
        "mcp": SERVICE_MIN_LEVEL["mcp"].label,
        "delegation_targets": SERVICE_MIN_LEVEL["delegation_targets"].label,
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
        text = "; ".join(level_description_parts(level))
        lines.append(f"- {_name(level)} ({LEVEL_TITLE[level]}): {text}")
    lines += [
        "",
        "Rules that do not depend on the level:",
        "- Approvals are separate: layered rules (installation, user, agent) decide what asks, what is allowed and what is "
        "denied. Hardline commands and protected paths are always refused.",
        "- Allowing everything on a real machine (a blanket allow rule, default allow or approvals off) needs the level admin.",
        "- The `local` backend and root ssh users are admin only; unprivileged ssh backends start at operator.",
        "- Unattended runs (dreams, scheduled tasks) deny whatever would ask; they never change permissions or agents.",
        "- An agent with the `permissions` group changes tool groups and approval rules of agents of its own user (itself "
        "included, never beyond its own level) and every change shows the user a card to approve once or deny. "
        "It sets the level of other agents up to its own level and the user's maximum, and may only lower its own (then it loses "
        "the group). The user can always change any level (Agents tab > level) up to their maximum level; only an administrator "
        "changes the maximum.",
        "- An agent with the `agents` group lists, creates, edits, renames and deletes agents of its own user, every change on a card "
        "to approve once or deny. A new agent gets at most the creator's level and the user's maximum; an agent above its own level "
        "is read-only for it; it never renames or deletes itself, and never changes levels, tool groups or rules (the `permissions` "
        "group does). Terminal backends, users and provider keys stay with the administrator.",
        "Recommend the lowest level that does the job.",
    ]
    return "\n".join(lines)


def _services_added(level: Level) -> list[str]:
    """The SERVICE_MIN_LEVEL keys whose minimum is exactly `level`, in table order."""
    if level == Level.MINIMAL:
        return []
    return [key for key, minimum in SERVICE_MIN_LEVEL.items() if minimum == level]
