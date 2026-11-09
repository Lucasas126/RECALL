"""Level ceilings: what an effective permission level may do, derived in one place.

Design: docs/architecture/permissions.md ("Levels and services" and "Dream mode"). Everything that builds or runs tools
asks this module instead of comparing levels itself: `build_tools`, `build_dream_tools`, `build_mcp_tools`, the worker
and delegate tools, and the two dispatch loops (chats and dreams). A level is a ceiling over independent services; the
agent's own configuration (tool groups, backends, MCP) can only narrow it further.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.agent.tool_groups import TOOL_GROUP_TOOLS
from src.config.levels import SERVICE_MIN_LEVEL, Level, backend_min_level

# the tool groups of a chat, by the lowest level that may hold them (`terminal` is the ssh class; the local backend and
# a root ssh user need more, see `backend_allowed`)
GROUP_MIN_LEVEL: dict[str, Level] = {
    "memory_read": SERVICE_MIN_LEVEL["memory_read"],
    "folders_read": SERVICE_MIN_LEVEL["folders_read"],
    "memory_edit": SERVICE_MIN_LEVEL["memory_edit"],
    "self": SERVICE_MIN_LEVEL["self"],
    "web": SERVICE_MIN_LEVEL["web"],
    "folders_edit": SERVICE_MIN_LEVEL["folders_edit"],
    "delegate": SERVICE_MIN_LEVEL["delegate"],
    "terminal": SERVICE_MIN_LEVEL["terminal_ssh"],
}

# reading the agent's own skills is how a skill is used at all, so it is never level-gated
ALWAYS_ALLOWED_TOOLS: frozenset[str] = frozenset({"skill_view"})
DREAM_REVIEW_TOOLS: tuple[str, ...] = ("dream_pending", "dream_read_chat", "dream_mark_reviewed")


def _tool_min_levels() -> dict[str, Level]:
    levels: dict[str, Level] = {}
    for group, names in TOOL_GROUP_TOOLS.items():
        for name in names:  # a tool in several groups (folder reads) is reachable at the lowest of them
            levels[name] = min(levels.get(name, Level.ADMIN), GROUP_MIN_LEVEL[group])
    for name in ALWAYS_ALLOWED_TOOLS:
        levels[name] = Level.MINIMAL
    return levels


TOOL_MIN_LEVEL: dict[str, Level] = _tool_min_levels()


class ToolNotAllowed(PermissionError):
    """A tool the agent's level does not cover (what the dispatch tells the model)."""


# ------------------------------------------------------------------ chats


def group_allowed(level: Level, group: str) -> bool:
    return group in GROUP_MIN_LEVEL and level >= GROUP_MIN_LEVEL[group]


def allowed_groups(level: Level) -> frozenset[str]:
    """The tool groups an agent of `level` may hold."""
    return frozenset(group for group in GROUP_MIN_LEVEL if level >= GROUP_MIN_LEVEL[group])


def mcp_allowed(level: Level) -> bool:
    return level >= SERVICE_MIN_LEVEL["mcp"]


def backend_allowed(level: Level, backend: Any) -> bool:
    """Terminal backend classes: ssh without root needs operator; the `local` backend and a root ssh user need admin."""
    return level >= backend_min_level(backend)


def usable_backends(level: Level, backends: Mapping[str, Any]) -> dict[str, Any]:
    """The backends (by name) an agent of `level` may use; the terminal group itself needs operator."""
    if not group_allowed(level, "terminal"):
        return {}
    return {name: backend for name, backend in backends.items() if backend_allowed(level, backend)}


def delegation_targets_allowed(level: Level) -> bool:
    """Handing tasks to the user's other agents."""
    return level >= SERVICE_MIN_LEVEL["delegation_targets"]


def workers_allowed(level: Level) -> bool:
    """Launching workers (the `delegate` group)."""
    return group_allowed(level, "delegate")


def tool_min_level(name: str, mcp_tools: frozenset[str] | set[str] = frozenset()) -> Level | None:
    """The lowest level that may call `name`; None for a name that is neither built in nor one of the MCP tools."""
    if name in TOOL_MIN_LEVEL:
        return TOOL_MIN_LEVEL[name]
    if name in mcp_tools:
        return SERVICE_MIN_LEVEL["mcp"]
    return None


def tool_allowed(level: Level, name: str, mcp_tools: frozenset[str] | set[str] = frozenset()) -> bool:
    """Whether a tool call is covered by `level` (unknown names are not)."""
    needed = tool_min_level(name, mcp_tools)
    return needed is not None and level >= needed


def refusal(level: Level, name: str, mcp_tools: frozenset[str] | set[str] = frozenset()) -> str:
    """The tool error for a call the level does not cover."""
    needed = tool_min_level(name, mcp_tools)
    if needed is None:
        return f"error: unknown tool '{name}'"
    return (
        f"error: '{name}' is not allowed at this agent's level '{level.label}' (it needs '{needed.label}' or higher); "
        "do not look for a way around this limit, tell the user which level would be required"
    )


def restrict_config(config: Any, level: Level | None = None) -> Any:
    """A copy of a ResolvedAgent limited to `level` (default its own): tool groups, MCP, backends and delegation.

    Used where a level can drop below the one the agent was loaded with (workers) and as a last gate before tools are
    built. Returns `config` itself when nothing changes.
    """
    own = config.level
    level = own if level is None else level if own is None else min(own, level)
    if level is None:
        raise ValueError(f"agent '{config.id}' has no effective level")
    update: dict[str, Any] = {}
    if config.level != level:
        update["level"] = level
    groups = config.tool_groups
    if groups is not None:
        kept = [group for group in groups if group_allowed(level, group)]
        if kept != list(groups):
            update["tool_groups"] = kept
    else:
        groups = []
    if config.mcp and not mcp_allowed(level):
        update["mcp"] = {}
    backends = usable_backends(level, config.terminal_backends) if config.terminal_backends else {}
    if len(backends) != len(config.terminal_backends):
        update["terminal_backends"] = backends
        terminal = config.terminal
        keep = [name for name in (terminal.backends if terminal is not None else {}) if name in backends]
        if terminal is not None and keep:
            update["terminal"] = terminal.model_copy(update={
                "backends": {name: terminal.backends[name] for name in keep},
                "default_backend": terminal.default_backend if terminal.default_backend in keep else keep[0],
            })
        else:
            update["terminal"] = None
    delegation = config.delegation
    if delegation is not None and delegation.targets and not delegation_targets_allowed(level):
        update["delegation"] = delegation.model_copy(update={"targets": []})
    return config.model_copy(update=update) if update else config


def worker_level(own: Level, launcher: Level | None) -> Level:
    """A worker never outranks the agent that launched it: the lower of its own level and the launcher's."""
    return own if launcher is None else min(own, launcher)


def grantable_groups(level: Level, groups: list[str]) -> list[str]:
    """The groups of a worker grant that the launcher's level covers (workers need the `delegate` group)."""
    if not workers_allowed(level):
        return []
    return [group for group in groups if group_allowed(level, group)]


# ------------------------------------------------------------------ dreams


@dataclass(frozen=True)
class DreamCapabilities:
    """What a dream session of an agent may do at a level (the dream table of the design). Unattended: no approvals."""

    level: Level
    enabled: bool               # a dream runs at all: level >= reader
    memory_read: bool           # query/search/read the mind palace
    memory_edit: bool           # write the mind palace (also needs the group)
    self_files: bool            # MEMORY, USER, SOUL, skills (also needs the group)
    folders_read: bool          # read-only, only assigned folders (also needs `dream.tools: [folders]`)
    web: bool                   # read only (also needs `dream.tools: [web]`)
    mcp_read_only: bool         # read-only MCP tools; the dream does not build them yet
    sandbox_read: bool          # sandbox_read / sandbox_search on disposable backends; the dream does not build them yet
    terminal_exec: bool = False  # never: no exec, sandbox_write or sandbox_patch in a dream
    limited_delegate: bool = False  # planned (read-only workers), not built in this step
    handoff: bool = False       # never: no tasks for other agents
    permissions: bool = False   # never


def dream_capabilities(level: Level) -> DreamCapabilities:
    return DreamCapabilities(
        level=level,
        enabled=level >= SERVICE_MIN_LEVEL["memory_read"],
        memory_read=level >= SERVICE_MIN_LEVEL["memory_read"],
        memory_edit=level >= SERVICE_MIN_LEVEL["memory_edit"],
        self_files=level >= SERVICE_MIN_LEVEL["self"],
        folders_read=level >= SERVICE_MIN_LEVEL["folders_read"],
        web=level >= SERVICE_MIN_LEVEL["web"],
        mcp_read_only=level >= SERVICE_MIN_LEVEL["mcp"],
        sandbox_read=level >= SERVICE_MIN_LEVEL["terminal_ssh"],
    )


def dream_allowed_tools(level: Level) -> frozenset[str]:
    """The tool names a dream of `level` may call: the review queue plus what the dream table allows. No terminal, no
    task tools, no write on the user's folders, whatever the level."""
    caps = dream_capabilities(level)
    names: set[str] = set(DREAM_REVIEW_TOOLS)
    if caps.memory_read:
        names.update(TOOL_GROUP_TOOLS["memory_read"])
    if caps.memory_edit:
        names.update(TOOL_GROUP_TOOLS["memory_edit"])
    if caps.self_files:
        names.update(TOOL_GROUP_TOOLS["self"])
    if caps.folders_read:
        names.update(TOOL_GROUP_TOOLS["folders_read"])
    if caps.web:
        names.update(TOOL_GROUP_TOOLS["web"])
    return frozenset(names)


def dream_refusal(level: Level, name: str) -> str:
    return (
        f"error: '{name}' is not allowed in a dream at this agent's level '{level.label}' "
        "(see the dream table of the permission levels)"
    )


# ------------------------------------------------------------------ summary


@dataclass(frozen=True)
class Capabilities:
    """Everything a level allows, in one value (for the prompt, the CLI and tests)."""

    level: Level
    groups: tuple[str, ...]
    mcp: bool
    terminal_machine: bool      # ssh backends without privileges (a sandbox or a real machine)
    terminal_privileged: bool   # the local backend and root ssh users
    delegation_workers: bool
    delegation_targets: bool
    dream: DreamCapabilities


def capabilities(level: Level) -> Capabilities:
    return Capabilities(
        level=level,
        groups=tuple(sorted(allowed_groups(level))),
        mcp=mcp_allowed(level),
        terminal_machine=group_allowed(level, "terminal"),
        terminal_privileged=level >= SERVICE_MIN_LEVEL["terminal_local"],
        delegation_workers=workers_allowed(level),
        delegation_targets=delegation_targets_allowed(level),
        dream=dream_capabilities(level),
    )
