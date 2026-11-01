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
)

TOOL_GROUP_IDS: Final[frozenset[str]] = frozenset(item["id"] for item in TOOL_GROUP_CATALOG)
DEFAULT_TOOL_GROUPS: Final[tuple[str, ...]] = tuple(item["id"] for item in TOOL_GROUP_CATALOG)

TOOL_GROUP_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "memory_read": (
        "query_memory", "search", "list_memories", "read", "list_tags", "find_notes_by_tag",
    ),
    "memory_edit": ("write", "append", "edit", "move", "delete"),
    "web": ("web_search", "web_retrieve"),
}
def validate_tool_groups(tool_groups: list[str]) -> list[str]:
    """Validate and normalize an agent's tool-group selection."""
    if len(set(tool_groups)) != len(tool_groups):
        raise ValueError("Tool-group selections must not contain duplicates")
    unknown = sorted(set(tool_groups) - TOOL_GROUP_IDS)
    if unknown:
        raise ValueError(f"unknown tool group: {', '.join(unknown)}")
    return tool_groups
