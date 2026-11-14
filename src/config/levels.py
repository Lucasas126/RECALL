"""Permission levels: the ordered presets a user's `max_level` and an agent's `level` choose from.

Design: docs/architecture/permissions.md. This module holds the schema side: the ordered names, the minimum level of
every service, what a configuration needs and the config rule (`check_agent_level`). What a level may *do* at run
time (tool groups, MCP, terminal classes, delegation, dreams) is derived in `src/config/ceiling.py`.
"""

from __future__ import annotations

from enum import IntEnum
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
    "notify": Level.ASSISTANT,
    "files_send": Level.READER,
    "automations": Level.ASSISTANT,
    "folders_edit": Level.COLLABORATOR,
    "delegate": Level.COLLABORATOR,
    "mcp": Level.COLLABORATOR,
    "permissions": Level.OPERATOR,
    "agents": Level.OPERATOR,
    "terminal_ssh": Level.OPERATOR,
    "delegation_targets": Level.OPERATOR,
    "terminal_local": Level.ADMIN,
    "terminal_root": Level.ADMIN,
}


class LevelExceedsMax(InputError):
    """An agent declares a level above the maximum its owner may give."""

    code = "level_exceeds_max"


class LevelBelowRequired(InputError):
    """An agent's level is lower than what one of its services needs."""

    code = "level_below_required"


class LevelRequiredAboveMax(InputError):
    """An agent is configured with a service that needs more than the owner's maximum level."""

    code = "level_required_above_max"


class ApprovalRuleNotAllowed(InputError):
    """An approval rule, default or shorthand that allows everything on a real machine, for an agent below admin."""

    code = "approval_rule_not_allowed"


APPROVAL_RULE_NOT_ALLOWED = (
    "Agent '{agent_id}' has the level '{level}', but {problem}. Allowing everything on a real machine needs the level "
    "'admin'. Narrow the rule (add a command, path or dangerous condition), use approvals 'manual' or 'smart', set "
    "kind: sandbox on the backend if it is a disposable sandbox, or ask for the level 'admin'."
)
LEVEL_EXCEEDS_MAX = (
    "The level '{level}' of agent '{agent_id}' is above your maximum level '{max_level}'. "
    "Choose '{max_level}' or lower, or ask an administrator to raise your maximum."
)
LEVEL_BELOW_REQUIRED = (
    "Agent '{agent_id}' has the level '{level}', but {service} needs the level '{needed}'. "
    "Raise the level of the agent to '{needed}' (at most your maximum) or remove {service}."
)
LEVEL_REQUIRED_ABOVE_MAX = (
    "Agent '{agent_id}' uses {service}, which needs the level '{needed}', above your maximum level '{max_level}'. "
    "Remove {service} or ask an administrator to raise your maximum."
)


def selected_tool_groups(agent: Any) -> list[str]:
    """The tool groups an agent runs with: its own list, the defaults when it sets none, nothing without tools.

    Accepts an AgentConfig (`chat_config`) and a ResolvedAgent (already resolved fields).
    """
    from src.agent.tool_groups import DEFAULT_TOOL_GROUPS

    if not hasattr(agent, "chat_config"):
        return list(agent.tool_groups or ()) if agent.tools else []
    chat = agent.chat_config
    if chat.tool_groups is not None:
        return list(chat.tool_groups)
    return list(DEFAULT_TOOL_GROUPS) if chat.tools else []


def backend_min_level(backend: Any) -> Level:
    """The lowest level that may use a terminal backend: `local` and an ssh `root` user are admin, any other ssh is operator."""
    if backend.type == "local":
        return SERVICE_MIN_LEVEL["terminal_local"]
    if getattr(backend, "user", None) == "root":
        return SERVICE_MIN_LEVEL["terminal_root"]
    return SERVICE_MIN_LEVEL["terminal_ssh"]


def required_level_reason(
    agent: Any, user_terminal_backends: Optional[Mapping[str, Any]] = None,
) -> tuple[Level, str]:
    """The lowest level that covers everything the agent is configured with, and the service that sets it.

    `agent` is an AgentConfig or a ResolvedAgent; `user_terminal_backends` maps backend names to the user's backend
    configs (`.type`, and `.user` for ssh). A backend name the user does not define is ignored (it never resolves),
    but selecting the `terminal` group at all needs at least the ssh level.
    """
    groups = selected_tool_groups(agent)
    needed, service = Level.MINIMAL, "no service"

    def raise_to(level: Level, label: str) -> None:
        nonlocal needed, service
        if level > needed:
            needed, service = level, label

    for group in groups:
        raise_to(SERVICE_MIN_LEVEL.get(group, Level.MINIMAL), f"tool group '{group}'")
    mcp = agent.mcp if not hasattr(agent, "chat_config") else agent.chat_config.mcp
    if mcp:
        raise_to(SERVICE_MIN_LEVEL["mcp"], "chat_config.mcp")
    if "terminal" in groups:
        raise_to(SERVICE_MIN_LEVEL["terminal_ssh"], "tool group 'terminal'")
        defined = user_terminal_backends if user_terminal_backends is not None else getattr(agent, "terminal_backends", {})
        for name in (agent.terminal.backends if agent.terminal is not None else {}):
            backend = (defined or {}).get(name)
            if backend is None:
                continue
            kind = "type local" if backend.type == "local" else "ssh user root"
            level = backend_min_level(backend)
            if level >= SERVICE_MIN_LEVEL["terminal_root"]:
                raise_to(level, f"terminal backend '{name}' ({kind})")
    delegation = agent.delegation
    if "delegate" in groups and delegation is not None and delegation.enabled and delegation.targets:
        raise_to(SERVICE_MIN_LEVEL["delegation_targets"], "delegation.targets")
    return needed, service


def required_level(agent: Any, user_terminal_backends: Optional[Mapping[str, Any]] = None) -> Level:
    """The lowest level that covers everything the agent is configured with (see `required_level_reason`)."""
    return required_level_reason(agent, user_terminal_backends)[0]


def effective_agent_level(agent: Any, user: Any) -> Level:
    """The agent's `level`: declared, else the lowest level covering its configuration."""
    if agent.level is not None:
        return agent.level
    return required_level(agent, user.terminal_backends)


def check_approval_rules(agent: Any, user: Any, installation: Any = None) -> None:
    """The blanket-allow guardrail: below admin, nothing may allow everything on a `machine` or `local` backend.

    Looks at the three rule layers and the per-backend `approvals` shorthand of the backends the agent uses. On a
    `sandbox` backend a blanket allow is fine from operator. `installation` defaults to settings.yaml `approval_rules`.
    Raises ApprovalRuleNotAllowed naming the offending rule.
    """
    from src.terminal.rules import RuleSet, blanket_allows

    if agent.terminal is None or "terminal" not in selected_tool_groups(agent):
        return
    level = effective_agent_level(agent, user)
    if level >= Level.ADMIN:
        return
    if installation is None:
        from src.config.settings import APPROVAL_RULES

        installation = APPROVAL_RULES
    defined = user.terminal_backends
    kinds = {name: defined[name].backend_kind for name in agent.terminal.backends if name in defined}
    shorthands = {name: agent.terminal.shorthand(name) for name in kinds}
    problems = blanket_allows(RuleSet.build(installation, user.approval_rules, agent.approval_rules), kinds, shorthands)
    if problems:
        raise ApprovalRuleNotAllowed(
            APPROVAL_RULE_NOT_ALLOWED, agent_id=agent.id, level=level.label, problem="; ".join(problems),
        )


def check_agent_level(agent: Any, user: Any, max_level: Level) -> None:
    """The config rule: required_level(agent) <= effective level <= the owner's `max_level`, else the agent is invalid.

    Raises LevelExceedsMax (declared level above the maximum), LevelRequiredAboveMax (the configuration needs more than
    the maximum, declared level or not) or LevelBelowRequired (declared level below what the configuration needs).
    """
    if agent.level is not None and agent.level > max_level:
        raise LevelExceedsMax(
            LEVEL_EXCEEDS_MAX, level=agent.level.label, agent_id=agent.id, max_level=max_level.label,
        )
    needed, service = required_level_reason(agent, user.terminal_backends)
    if needed > max_level:
        raise LevelRequiredAboveMax(
            LEVEL_REQUIRED_ABOVE_MAX, agent_id=agent.id, service=service, needed=needed.label, max_level=max_level.label,
        )
    if agent.level is not None and agent.level < needed:
        raise LevelBelowRequired(
            LEVEL_BELOW_REQUIRED, agent_id=agent.id, level=agent.level.label, service=service, needed=needed.label,
        )
    check_approval_rules(agent, user)
