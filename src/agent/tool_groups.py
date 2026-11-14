from __future__ import annotations

from typing import Final

TOOL_GROUP_CATALOG: Final[tuple[dict[str, str], ...]] = (
    {
        "id": "memory_read",
        "name": "Memory read-only",
        "description": "Search, list and read the agent's sources, and look at images and PDF pages.",
    },
    {
        "id": "memory_edit",
        "name": "Memory read and edit",
        "description": "Create, edit, move and delete memories, and make PDF files.",
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
        "id": "notify",
        "name": "Telegram notices",
        "description": "Send you a short message on Telegram (for example when a long task is done).",
    },
    {
        "id": "files_send",
        "name": "Send files",
        "description": "Send you a file the agent can already read (a note, a document, an image), in the web chat and on Telegram.",
    },
    {
        "id": "automations",
        "name": "Automations",
        "description": "Schedule things for later: once, every N minutes, on a cron schedule or when a background process ends.",
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
    {
        "id": "permissions",
        "name": "Manage permissions",
        "description": "View and change the levels, tool groups and approval rules of your agents (yours too: groups and rules within your level, your level only lower), each change with your approval.",
    },
    {
        "id": "agents",
        "name": "Manage agents",
        "description": "List, create, edit, rename and delete your agents (not their level, tool groups or rules), each change with your approval.",
    },
)

TOOL_GROUP_IDS: Final[frozenset[str]] = frozenset(item["id"] for item in TOOL_GROUP_CATALOG)
# used when an agent sets no tool_groups; folder access is always an explicit choice
DEFAULT_TOOL_GROUPS: Final[tuple[str, ...]] = ("memory_read", "memory_edit", "web", "self")

# the `permissions` group: reads, then the mutations (each one asks the user, once, and is audited)
PERMISSION_READ_TOOLS: Final[tuple[str, ...]] = ("agents_list", "agent_get_permissions", "permission_levels_help")
PERMISSION_CHANGE_TOOLS: Final[tuple[str, ...]] = (
    "agent_set_level", "agent_set_tool_groups", "agent_add_rule", "agent_remove_rule", "agent_reset_rules",
)
PERMISSION_TOOLS: Final[tuple[str, ...]] = (*PERMISSION_READ_TOOLS, *PERMISSION_CHANGE_TOOLS)

# the `agents` group: reads, then the mutations (each one asks the user, once, and is audited). Singular `agent_*` names;
# the `permissions` group owns `agents_list` and the `agent_set_*` / `agent_*_rule(s)` ones
AGENT_READ_TOOLS: Final[tuple[str, ...]] = ("agent_list", "agent_get", "agent_templates")
AGENT_CHANGE_TOOLS: Final[tuple[str, ...]] = ("agent_create", "agent_update", "agent_rename", "agent_delete")
AGENT_TOOLS: Final[tuple[str, ...]] = (*AGENT_READ_TOOLS, *AGENT_CHANGE_TOOLS)

TOOL_GROUP_TOOLS: Final[dict[str, tuple[str, ...]]] = {
    "memory_read": (
        "query_memory", "search", "list_memories", "read", "list_tags", "find_notes_by_tag", "session_search", "view_image",
    ),
    "memory_edit": ("write", "append", "edit", "move", "delete", "make_pdf"),
    "web": ("web_search", "web_retrieve"),
    "self": ("memory", "skill_view", "skill_manage", "soul_edit"),
    "folders_read": ("folder_list", "folder_read", "folder_search"),
    "folders_edit": (
        "folder_list", "folder_read", "folder_search",
        "folder_write", "folder_append", "folder_edit", "folder_move", "folder_delete",
    ),
    "notify": ("notify_telegram",),
    "files_send": ("send_file",),
    "automations": (
        "automation_create", "automation_list", "automation_edit", "automation_pause", "automation_resume", "automation_delete",
    ),
    "terminal": (
        "terminal", "sandbox_read", "sandbox_write", "sandbox_patch", "sandbox_search",
        "process_start", "process_list", "process_read", "process_wait", "process_stop",
    ),
    "delegate": ("task_start", "task_list", "task_result", "task_steer", "task_send", "task_stop", "task_wait"),
    "permissions": PERMISSION_TOOLS,
    "agents": AGENT_TOOLS,
}

# the groups a parent may hand to a subagent (phase 1: read-only web access); memory, self, folders, terminal,
# MCP and `delegate` itself are never grantable
DELEGATE_GRANTABLE_GROUPS: Final[tuple[str, ...]] = ("web",)

# groups an *automation's* unattended task also keeps: it works while the owner is away, so it must be able to tell them
# (`notify_telegram`) or hand them a file (`send_file`, here always to their Telegram). Nothing else is added.
AUTOMATION_EXTRA_GROUPS: Final[tuple[str, ...]] = ("notify", "files_send")

# tools that change the agent's own persistent state (memory, SOUL, skills, notes, user folders);
# a disposable chat neither exposes nor runs them
DISPOSABLE_BLOCKED_TOOLS: Final[frozenset[str]] = frozenset(
    {*TOOL_GROUP_TOOLS["memory_edit"], "memory", "skill_manage", "soul_edit"}
    | {name for name in TOOL_GROUP_TOOLS["folders_edit"] if name not in TOOL_GROUP_TOOLS["folders_read"]}
    | set(TOOL_GROUP_TOOLS["terminal"])  # a disposable chat never gets the terminal at all
    | set(TOOL_GROUP_TOOLS["delegate"])  # nor can it launch tasks: this also gives depth 1 (children are disposable)
    | set(TOOL_GROUP_TOOLS["permissions"])  # nor change anyone's permissions: workers never hold that group
    | set(TOOL_GROUP_TOOLS["agents"])  # nor create, change or delete agents: same
    | set(TOOL_GROUP_TOOLS["notify"])  # nor write to the user's phone
    | set(TOOL_GROUP_TOOLS["files_send"])  # nor hand files to the user
    | set(TOOL_GROUP_TOOLS["automations"])  # nor schedule anything: an automation's own task cannot create more automations
)


def validate_tool_groups(tool_groups: list[str]) -> list[str]:
    """Validate and normalize an agent's tool-group selection."""
    if len(set(tool_groups)) != len(tool_groups):
        raise ValueError("Tool-group selections must not contain duplicates")
    unknown = sorted(set(tool_groups) - TOOL_GROUP_IDS)
    if unknown:
        raise ValueError(f"unknown tool group: {', '.join(unknown)}")
    return tool_groups
