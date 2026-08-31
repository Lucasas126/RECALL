from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.agent import Agent

logger = get_logger("tools")


def build_tools(agent: "Agent") -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """Build the OpenAI-style tool specs and their handlers, bound to *agent*.

    Handlers return a plain string (what the model sees back). Read/write also
    keep the vector index in sync with the touched source.
    """
    sources = agent.sources

    def query_memory(query: str, top_k: int = 5) -> str:
        hits = agent.query_memory(query, top_k)
        if not hits:
            return "no matching memories."
        return "\n".join(f"[{h['address']}] {h['document']}" for h in hits)

    def read(address: str) -> str:
        name = address.split(":", 1)[0]
        agent.index.sync_source(name)  # reindex-on-read: catch external edits
        return sources.read(address)

    def write(address: str, content: str) -> str:
        path = sources.write(address, content)
        name = address.split(":", 1)[0]
        agent.index.sync_source(name)  # reindex-on-edit
        return f"wrote {path}"

    def list_(source: str = "", subpath: str = "") -> str:
        entries = sources.list(source, subpath)
        return "\n".join(entries) if entries else "(empty)"

    handlers: dict[str, Callable[..., str]] = {
        "query_memory": query_memory,
        "read": read,
        "write": write,
        "list": list_,
    }

    specs = [
        {
            "type": "function",
            "function": {
                "name": "query_memory",
                "description": "Semantically search your own memory (past conversations and notes). Use this to recall things the user told you before.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "what to look for, in natural language"},
                        "top_k": {"type": "integer", "description": "how many results to return (default 5)"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "Read a memory file by address ('source:path', e.g. 'mind_palace:notes/pets.md').",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "the 'source:path' address to read"},
                    },
                    "required": ["address"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write",
                "description": "Write a memory file by address ('source:path'). Use this to remember something durably.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "the 'source:path' address to write"},
                        "content": {"type": "string", "description": "the full file contents"},
                    },
                    "required": ["address", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list",
                "description": "List available memory sources, or the entries inside one source.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "source": {"type": "string", "description": "source name; omit to list all sources"},
                        "subpath": {"type": "string", "description": "folder inside the source"},
                    },
                    "required": [],
                },
            },
        },
    ]

    return specs, handlers
