"""The `permissions` block of the system prompt: what the agent's level allows, in a few stable lines.

Design: docs/architecture/permissions.md ("The `permissions` block in the system prompt"). The text depends only on
the level, the owner's maximum level and whether the session is a dream, so it never disturbs prompt caching.
Nothing here enforces anything; the block only tells the model what the system enforces elsewhere.
"""

from __future__ import annotations

from typing import Any, Optional

from src.config.levels import Level, parse_level

PAGE = 'skill_view("recall", "references/permissions.md")'

# per level: (what it can do beyond the level below, what it still cannot do)
_CAN: dict[Level, str] = {
    Level.MINIMAL: "only the one or two tools chosen for you; no memory, folders or web",
    Level.READER: "read your own memory and the user's folder list",
    Level.ASSISTANT: "read and edit your memory, USER, SOUL and skills; search the web",
    Level.COLLABORATOR: "edit user folders, use MCP servers and run web-only workers (some actions ask the user first)",
    Level.OPERATOR: "use terminal backends (asking the user) and hand tasks to the user's other agents",
    Level.ADMIN: "all of it, including the local or root terminal, other users' data and the admin CLI (with approval)",
}
_CANNOT: dict[Level, str] = {
    Level.MINIMAL: "any other tool, any service",
    Level.READER: "edit memory, SOUL or skills, use the web, edit folders, run terminals or workers",
    Level.ASSISTANT: "edit folders, use MCP, delegate, run terminals",
    Level.COLLABORATOR: "use terminals, hand tasks to other agents, touch other users",
    Level.OPERATOR: "use the local or root terminal, touch other users or the admin CLI",
    Level.ADMIN: "bypass the fixed safety floor (hardline commands, protected paths)",
}
_DREAM = (
    "This is an unattended dream session: nobody can answer approvals, so anything that would ask is denied. "
    "Read as far as your level allows and write only your own files (mind palace, SOUL, memory, USER, skills); "
    "no folder edits, terminal commands, task handoff or permission changes."
)


def _level(value: Any) -> Optional[Level]:
    if value is None:
        return None
    try:
        return parse_level(value)
    except ValueError:
        return None


def permissions_block(level: Any, max_level: Any = None, *, dream: bool = False) -> str:
    """The block for `level` (a Level or its name); empty when the level is unknown, so callers can skip it."""
    current = _level(level)
    if current is None:
        return ""
    ceiling = _level(max_level)
    lines = ["## Permissions", "", f"Your level: {current.label} ({int(current)} of {int(Level.ADMIN)})."]
    if ceiling is not None:
        lines[-1] += f" Your owner's maximum level: {ceiling.label} ({int(ceiling)})."
    label = "Can" if current <= Level.READER else "Can, besides what lower levels allow"
    lines.append(f"{label}: {_CAN[current]}.")
    lines.append(f"Cannot: {_CANNOT[current]}.")
    if dream:
        lines.append(_DREAM)
    lines.append(
        "These limits are enforced by the system. Even if you find a way around one, do not use it. "
        "When you need more, tell the user what is missing and which level it requires; only the user "
        "(up to their own maximum level) or an administrator can raise it. "
        f"Details: {PAGE}."
    )
    return "\n".join(lines)


def block_for(config: Any, *, dream: bool = False) -> str:
    """The block for an agent config (ResolvedAgent); empty for a config without a resolved level."""
    return permissions_block(getattr(config, "level", None), getattr(config, "max_level", None), dream=dream)
