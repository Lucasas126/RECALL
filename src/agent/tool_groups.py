from __future__ import annotations

from typing import Final

TOOL_GROUP_CATALOG: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "memory_read",
        "name": "Memory read-only",
        "description": "Search, list and read the agent's sources.",
    },
    {
        "id": "memory_edit",
        "name": "Memory read and edit",
        "description": "Create, edit, move and delete memories.",
    },
    {
        "id": "web",
        "name": "Web search",
        "description": "Search and retrieve public web pages.",
    },
    {
        "id": "self",
        "name": "Self-improvement",
        "description": "Edit the agent's own SOUL.md, memories and skills.",
    },
    {
        "id": "folders_read",
        "name": "Folders read-only",
        "description": "List, read and search the agent's user folders.",
    },
    {
        "id": "folders_edit",
        "name": "Folders read and edit",
        "description": "Create, edit, move and delete files in the agent's user folders that are not read-only.",
    },
    {
        "id": "terminal",
        "name": "Terminal sandbox",
        "description": "Run commands and edit files in a sandbox, with your approval.",
    },
    {
        "id": "delegate",
        "name": "Delegate subtasks",
        "description": "Hand a self-contained task to a short-lived worker in a hidden chat and get its result back.",
    },
)

TOOL_GROUP_IDS: Final[frozenset[str]] = frozenset(item["id"] for item in TOOL_GROUP_CATALOG)
# used when an agent sets no tool_groups; folder access is always an explicit choice
DEFAULT_TOOL_GROUPS: Final[tuple[str, ...]] = ("memory_read", "memory_edit", "web", "self")

TOOL_GROUP_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "memory_read": (
        "query_memory", "search", "list_memories", "read", "list_tags", "find_notes_by_tag",
    ),
    "memory_edit": ("write", "append", "edit", "move", "delete"),
    "web": ("web_search", "web_retrieve"),
    "self": ("memory", "skill_view", "skill_manage", "soul_edit"),
    "folders_read": ("folder_list", "folder_read", "folder_search"),
    "folders_edit": (
        "folder_list", "folder_read", "folder_search",
        "folder_write", "folder_append", "folder_edit", "folder_move", "folder_delete",
    ),
    "terminal": ("terminal", "sandbox_read", "sandbox_write", "sandbox_patch", "sandbox_search"),
    "delegate": ("task_start",),
}

# the groups a parent may hand to a subagent (phase 1: read-only web access); memory, self, folders, terminal,
# MCP and `delegate` itself are never grantable
DELEGATE_GRANTABLE_GROUPS: Final[tuple[str, ...]] = ("web",)

# tools that change the agent's own persistent state (memory, SOUL, skills, notes, user folders);
# a disposable chat neither exposes nor runs them
DISPOSABLE_BLOCKED_TOOLS: Final[frozenset[str]] = frozenset(
    {*TOOL_GROUP_TOOLS["memory_edit"], "memory", "skill_manage", "soul_edit"}
    | {name for name in TOOL_GROUP_TOOLS["folders_edit"] if name not in TOOL_GROUP_TOOLS["folders_read"]}
    | set(TOOL_GROUP_TOOLS["terminal"])  # a disposable chat never gets the terminal at all
    | set(TOOL_GROUP_TOOLS["delegate"])  # nor can it launch tasks: this also gives depth 1 (children are disposable)
)


def validate_tool_groups(tool_groups: list[str]) -> list[str]:
    """Validate and normalize an agent's tool-group selection."""
    if len(set(tool_groups)) != len(tool_groups):
        raise ValueError("Tool-group selections must not contain duplicates")
    unknown = sorted(set(tool_groups) - TOOL_GROUP_IDS)
    if unknown:
        raise ValueError(f"unknown tool group: {', '.join(unknown)}")
    return tool_groups
