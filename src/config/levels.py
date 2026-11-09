"""Permission levels: the ordered presets a user's `max_level` and an agent's `level` choose from.

Design: docs/architecture/permissions.md. This module only holds the schema side (step 1): the ordered names,
the minimum level of every service and the helpers that resolve a missing value. Nothing here filters tools.
"""

from __future__ import annotations

import re
from enum import IntEnum
from pathlib import Path
from typing import Annotated, Any, Mapping, Optional

from pydantic import BeforeValidator, PlainSerializer

from src.utils.i18n import InputError


class Level(IntEnum):
    """Ordered permission levels; the YAML value is the lowercase name."""

    MINIMAL = 0
    READER = 1
    ASSISTANT = 2
    COLLABORATOR = 3
    OPERATOR = 4
    ADMIN = 5

    @property
    def label(self) -> str:
        return self.name.lower()

    def __str__(self) -> str:
        return self.label


LEVEL_NAMES: tuple[str, ...] = tuple(level.label for level in Level)


def parse_level(value: object) -> Level:
    """A `Level` from a level, or from its name in any case; anything else (numbers included) is an error."""
    if isinstance(value, Level):
        return value
    if isinstance(value, str):
        try:
            return Level[value.strip().upper()]
        except KeyError:
            pass
    raise ValueError(f"unknown level {value!r}: use one of {', '.join(LEVEL_NAMES)}")


# a pydantic field type: validated from the (case-insensitive) name, written back as the lowercase name
LevelField = Annotated[
    Level,
    BeforeValidator(parse_level),
    PlainSerializer(lambda level: level.label, return_type=str, when_used="always"),
]

# minimum level per service (docs/architecture/permissions.md, "Levels and services")
SERVICE_MIN_LEVEL: dict[str, Level] = {
    "memory_read": Level.READER,
    "folders_read": Level.READER,
    "memory_edit": Level.ASSISTANT,
    "self": Level.ASSISTANT,
    "web": Level.ASSISTANT,
    "folders_edit": Level.COLLABORATOR,
    "delegate": Level.COLLABORATOR,
    "mcp": Level.COLLABORATOR,
    "terminal_ssh": Level.OPERATOR,
    "delegation_targets": Level.OPERATOR,
    "terminal_local": Level.ADMIN,
    "terminal_root": Level.ADMIN,
}


class LevelExceedsMax(InputError):
    """An agent declares a level above the maximum its owner may give."""

    code = "level_exceeds_max"


LEVEL_EXCEEDS_MAX = (
    "The level '{level}' of agent '{agent_id}' is above your maximum level '{max_level}'. "
    "Choose '{max_level}' or lower, or ask an administrator to raise your maximum."
)


def selected_tool_groups(agent: Any) -> list[str]:
    """The tool groups an agent runs with: its own list, the defaults when it sets none, nothing without tools."""
    from src.agent.tool_groups import DEFAULT_TOOL_GROUPS

    chat = agent.chat_config
    if chat.tool_groups is not None:
        return list(chat.tool_groups)
    return list(DEFAULT_TOOL_GROUPS) if chat.tools else []


def required_level(agent: Any, user_terminal_backends: Optional[Mapping[str, Any]] = None) -> Level:
    """The lowest level that covers everything the agent is configured with.

    `agent` is an AgentConfig; `user_terminal_backends` maps backend names to the user's backend configs (`.type`,
    and `.user` for ssh). A backend name the user does not define is ignored (it never resolves), but selecting
    the `terminal` group at all needs at least the ssh level.
    """
    groups = selected_tool_groups(agent)
    needed = Level.MINIMAL
    for group in groups:
        needed = max(needed, SERVICE_MIN_LEVEL.get(group, Level.MINIMAL))
    if agent.chat_config.mcp:
        needed = max(needed, SERVICE_MIN_LEVEL["mcp"])
    if "terminal" in groups:
        needed = max(needed, SERVICE_MIN_LEVEL["terminal_ssh"])
        names = agent.terminal.backends if agent.terminal is not None else {}
        for name in names:
            backend = (user_terminal_backends or {}).get(name)
            if backend is None:
                continue
            if backend.type == "local":
                needed = max(needed, SERVICE_MIN_LEVEL["terminal_local"])
            elif getattr(backend, "user", None) == "root":
                needed = max(needed, SERVICE_MIN_LEVEL["terminal_root"])
    delegation = agent.delegation
    if "delegate" in groups and delegation is not None and delegation.enabled and delegation.targets:
        needed = max(needed, SERVICE_MIN_LEVEL["delegation_targets"])
    return needed


_USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def installation_user_count(config_dir: Path) -> int:
    """How many users the installation has: folders of `config_dir` holding a `user.yaml`."""
    if not config_dir.is_dir():
        return 0
    return sum(1 for item in config_dir.iterdir() if _USER_ID_RE.match(item.name) and (item / "user.yaml").is_file())


_warned: set[str] = set()


def forget_warnings() -> None:
    """Forget which warnings were already logged (tests)."""
    _warned.clear()


def _warn_once(key: str, message: str, *args: object) -> None:
    if key not in _warned:
        _warned.add(key)
        from src.utils.logger import get_logger  # lazy: the logger needs settings, which import the models

        get_logger("config").warning(message, *args)


def effective_user_level(user: Any, installation_users: int, *, warn: bool = True) -> Level:
    """The user's `max_level`: declared, else admin on a single-user installation, else collaborator (with a warning)."""
    if user.max_level is not None:
        return user.max_level
    level = Level.ADMIN if installation_users == 1 else Level.COLLABORATOR
    if not warn:
        return level
    _warn_once(
        f"max_level:{user.id}",
        "user %s has no max_level; using %s. Run `python -m src.admin migrate-levels` or "
        "`python -m src.admin user set --user %s --set max_level=<level>` to set it.",
        user.id, level.label, user.id,
    )
    return level


def effective_agent_level(agent: Any, user: Any) -> Level:
    """The agent's `level`: declared, else the lowest level covering its configuration."""
    if agent.level is not None:
        return agent.level
    return required_level(agent, user.terminal_backends)


def check_agent_level(agent: Any, user: Any, max_level: Level) -> None:
    """Raise LevelExceedsMax when the agent declares a level above `max_level`; warn (once) when an undeclared one needs more."""
    if agent.level is not None:
        if agent.level > max_level:
            raise LevelExceedsMax(
                LEVEL_EXCEEDS_MAX, level=agent.level.label, agent_id=agent.id, max_level=max_level.label,
            )
        return
    required = required_level(agent, user.terminal_backends)
    if required > max_level:
        _warn_once(
            f"required:{user.id}:{agent.id}:{required.label}:{max_level.label}",
            "agent %s/%s has no level and its configuration needs %s, above the user's max_level %s; "
            "a later release will enforce this. Lower its tools or ask an administrator to raise max_level.",
            user.id, agent.id, required.label, max_level.label,
        )
