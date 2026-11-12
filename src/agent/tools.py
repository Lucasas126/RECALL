from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from src.agent.toolsets.agents import build_agent_tools
from src.agent.toolsets.delegate import build_delegate_tools
from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.memory import build_memory_tools
from src.agent.toolsets.permissions import build_permission_tools
from src.agent.toolsets.self_files import build_self_tools
from src.agent.toolsets.sessions import build_session_tools
from src.agent.toolsets.tags import build_tag_tools
from src.agent.toolsets.folders import build_folder_tools
from src.agent.toolsets.terminal import build_terminal_tools
from src.agent.toolsets.web import build_web_tools
from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_TOOLS, validate_tool_groups
from src.config.ceiling import group_allowed, restrict_config, tool_allowed
from src.config.models import ResolvedAgent
from src.providers.tavily import TavilyClient

if TYPE_CHECKING:
    from src.agent.agent import Agent

_ORDER = (
    "query_memory", "search", "list_memories", "list_tags", "find_notes_by_tag",
    "session_search", "read", "write", "append", "edit", "move", "delete",
    "memory", "skill_view", "skill_manage", "soul_edit",
    "folder_list", "folder_read", "folder_search",
    "folder_write", "folder_append", "folder_edit", "folder_move", "folder_delete",
    "web_search", "web_retrieve",
    "terminal", "sandbox_read", "sandbox_write", "sandbox_patch", "sandbox_search",
    "process_start", "process_list", "process_read", "process_wait", "process_stop",
    "task_start", "task_list", "task_result", "task_steer", "task_send", "task_stop", "task_wait",
    "agents_list", "agent_get_permissions", "permission_levels_help",
    "agent_set_level", "agent_set_tool_groups", "agent_add_rule", "agent_remove_rule", "agent_reset_rules",
    "agent_list", "agent_get", "agent_templates", "agent_create", "agent_update", "agent_rename", "agent_delete",
)

TOOL_ORDER = _ORDER  # every built-in tool name; MCP tools must not reuse one


def chat_folder_options(config: ResolvedAgent) -> dict:
    """Registry options for a chat: every folder is registered; the folder tool groups decide access."""
    docs = {"recall_docs": config.recall_knowledge}
    if not config.folders:
        return docs
    groups = set(config.tool_groups or ()) if config.tools else set()
    return {
        **docs,
        "folders": config.folders,
        "folders_readable": bool(groups & {"folders_read", "folders_edit"}),
        "folders_writable": "folders_edit" in groups,
    }


def build_tools(
    agent: Agent, web_client: TavilyClient | None = None,
    tool_groups: list[str] | None = None,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    *, skill_view: bool = False, disposable: bool = False, worker_terminal: bool = False,
) -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """Build the tool specs and handlers bound to an agent.

    `skill_view` adds that tool even without the `self` group, so agents can always load their skills.
    `disposable` drops every tool that writes the agent's own persistent state (read tools stay).
    `worker_terminal` (a subagent task's chat) keeps the terminal group it was granted although the chat is disposable:
    being excluded from learning does not block the terminal (its approvals are manual, see `Agent._become_worker`).

    The agent's level is a ceiling (`src/config/ceiling.py`): groups, backends and tools it does not cover are dropped,
    whatever the configuration or the grant says.
    """
    level = agent.config.level
    selected = [group for group in validate_tool_groups(tool_groups or []) if group_allowed(level, group)]
    base_tools = []
    if "memory_read" in selected:
        base_tools.extend((*build_memory_tools(agent), *build_tag_tools(agent), *build_session_tools(agent)))
    if "memory_edit" in selected:
        base_tools.extend(build_edit_tools(agent))
    if "self" in selected or skill_view:
        self_tools = build_self_tools(agent)
        base_tools.extend(self_tools if "self" in selected else [
            func for func in self_tools if func.tool_spec["function"]["name"] == "skill_view"
        ])
    if {"folders_read", "folders_edit"} & set(selected) and agent.sources.folders():
        base_tools.extend(build_folder_tools(agent, writable="folders_edit" in selected and not disposable))
    if web_client is not None and "web" in selected:
        base_tools.extend(build_web_tools(web_client))
    if "terminal" in selected and (not disposable or worker_terminal):
        base_tools.extend(build_terminal_tools(restrict_config(agent.config)))
    if "delegate" in selected:  # a disposable chat (and so a task's own chat) never launches tasks: DISPOSABLE_BLOCKED_TOOLS
        base_tools.extend(build_delegate_tools(restrict_config(agent.config)))
    if "permissions" in selected:  # not in a disposable chat (workers and tasks): DISPOSABLE_BLOCKED_TOOLS
        base_tools.extend(build_permission_tools(restrict_config(agent.config)))
    if "agents" in selected:  # same: DISPOSABLE_BLOCKED_TOOLS
        base_tools.extend(build_agent_tools(restrict_config(agent.config)))
    tools_by_name = {
        func.tool_spec["function"]["name"]: func
        for func in base_tools
    }
    blocked = DISPOSABLE_BLOCKED_TOOLS - (set(TOOL_GROUP_TOOLS["terminal"]) if worker_terminal else set())
    denied = set(deny or ()) | (blocked if disposable else set())
    allowed = set(allow) if allow is not None else None
    funcs = [
        tools_by_name[name] for name in _ORDER
        if name in tools_by_name
        and tool_allowed(level, name)
        and name not in denied
        and (allowed is None or name in allowed)
    ]
    specs = [func.tool_spec for func in funcs]
    handlers = {func.tool_spec["function"]["name"]: func for func in funcs}
    return specs, handlers
