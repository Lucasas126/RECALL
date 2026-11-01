from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.memory import build_memory_tools
from src.agent.toolsets.tags import build_tag_tools
from src.agent.toolsets.web import build_web_tools
from src.agent.tool_groups import validate_tool_groups
from src.providers.tavily import TavilyClient

if TYPE_CHECKING:
    from src.agent.agent import Agent


def build_tools(
    agent: Agent, web_client: TavilyClient | None = None,
    tool_groups: list[str] | None = None,
    allow: list[str] | None = None,
    deny: list[str] | None = None,
) -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """Build the tool specs and handlers bound to an agent."""
    selected = validate_tool_groups(tool_groups or [])
    base_tools = []
    if "memory_read" in selected:
        base_tools.extend((*build_memory_tools(agent), *build_tag_tools(agent)))
    if "memory_edit" in selected:
        base_tools.extend(build_edit_tools(agent))
    tools_by_name = {
        func.tool_spec["function"]["name"]: func
        for func in base_tools
    }
    order = [
        "query_memory", "search", "list_memories", "list_tags", "find_notes_by_tag",
        "read", "write", "append", "edit", "move", "delete",
    ]
    if web_client is not None and "web" in selected:
        web_tools = build_web_tools(web_client)
        tools_by_name.update({
            func.tool_spec["function"]["name"]: func for func in web_tools
        })
        order.extend(["web_search", "web_retrieve"])
    denied = set(deny or ())
    allowed = set(allow) if allow is not None else None
    funcs = [
        tools_by_name[name] for name in order
        if name in tools_by_name
        and name not in denied
        and (allowed is None or name in allowed)
    ]
    specs = [func.tool_spec for func in funcs]
    handlers = {func.tool_spec["function"]["name"]: func for func in funcs}
    return specs, handlers
