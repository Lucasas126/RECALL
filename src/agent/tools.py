from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.memory import build_memory_tools
from src.agent.toolsets.tags import build_tag_tools
from src.agent.toolsets.web import build_web_tools
from src.providers.tavily import TavilyClient

if TYPE_CHECKING:
    from src.agent.agent import Agent


def build_tools(
    agent: Agent, web_client: TavilyClient | None = None
) -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """Build the tool specs and handlers bound to an agent."""
    base_tools = [
        *build_memory_tools(agent),
        *build_tag_tools(agent),
        *build_edit_tools(agent),
    ]
    tools_by_name = {
        func.tool_spec["function"]["name"]: func
        for func in base_tools
    }
    order = [
        "query_memory", "search", "list_memories", "list_tags", "find_notes_by_tag",
        "read", "write", "append", "edit", "move", "delete",
    ]
    if web_client is not None:
        web_tools = build_web_tools(web_client)
        tools_by_name.update({
            func.tool_spec["function"]["name"]: func for func in web_tools
        })
        order.extend(["web_search", "web_retrieve"])
    funcs = [tools_by_name[name] for name in order]
    specs = [func.tool_spec for func in funcs]
    handlers = {func.tool_spec["function"]["name"]: func for func in funcs}
    return specs, handlers
