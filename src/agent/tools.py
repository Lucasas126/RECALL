from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.memory import build_memory_tools
from src.agent.toolsets.self_files import build_self_tools
from src.agent.toolsets.tags import build_tag_tools
from src.agent.toolsets.vault import build_vault_tools
from src.agent.toolsets.web import build_web_tools
from src.agent.tool_groups import validate_tool_groups
from src.config.models import ResolvedAgent
from src.providers.tavily import TavilyClient

if TYPE_CHECKING:
    from src.agent.agent import Agent

_ORDER = (
    "query_memory", "search", "list_memories", "list_tags", "find_notes_by_tag",
    "read", "write", "append", "edit", "move", "delete",
    "memory", "skill_view", "skill_manage", "soul_edit",
    "vault_list", "vault_read", "vault_search",
    "vault_write", "vault_append", "vault_edit", "vault_move", "vault_delete",
    "web_search", "web_retrieve",
)


def chat_vault_options(config: ResolvedAgent) -> dict:
    """Registry options that expose the agent's vault to its chats, per its vault tool groups."""
    groups = set(config.tool_groups or ())
    if not config.vault or not config.tools or not groups & {"vault_read", "vault_edit"}:
        return {}
    return {"vault": config.vault, "vault_writable": "vault_edit" in groups}


def build_tools(
    agent: Agent, web_client: TavilyClient | None = None,
    tool_groups: list[str] | None = None,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
    *, skill_view: bool = False,
) -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """Build the tool specs and handlers bound to an agent.

    `skill_view` adds that tool even without the `self` group, so agents can always load their skills.
    """
    selected = validate_tool_groups(tool_groups or [])
    base_tools = []
    if "memory_read" in selected:
        base_tools.extend((*build_memory_tools(agent), *build_tag_tools(agent)))
    if "memory_edit" in selected:
        base_tools.extend(build_edit_tools(agent))
    if "self" in selected or skill_view:
        self_tools = build_self_tools(agent)
        base_tools.extend(self_tools if "self" in selected else [
            func for func in self_tools if func.tool_spec["function"]["name"] == "skill_view"
        ])
    if {"vault_read", "vault_edit"} & set(selected) and agent.sources.vault() is not None:
        base_tools.extend(build_vault_tools(agent, writable="vault_edit" in selected))
    if web_client is not None and "web" in selected:
        base_tools.extend(build_web_tools(web_client))
    tools_by_name = {
        func.tool_spec["function"]["name"]: func
        for func in base_tools
    }
    denied = set(deny or ())
    allowed = set(allow) if allow is not None else None
    funcs = [
        tools_by_name[name] for name in _ORDER
        if name in tools_by_name
        and name not in denied
        and (allowed is None or name in allowed)
    ]
    specs = [func.tool_spec for func in funcs]
    handlers = {func.tool_spec["function"]["name"]: func for func in funcs}
    return specs, handlers
