from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Callable, Optional

from src.agent.tooling import tool
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.agent import Agent

logger = get_logger("tools")


def build_tools(agent: "Agent") -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """Build the OpenAI-style tool specs and their handlers, bound to *agent*.

    Each tool is a plain typed function; its schema is derived from the signature,
    ``Annotated`` parameter descriptions and docstring (see ``@tool``). Handlers
    return a string (what the model sees back); writes keep the index in sync.
    """
    sources = agent.sources

    @tool
    def query_memory(
        query: Annotated[str, "key concepts/entities to match (keywords or a short noun phrase), not a full sentence"],
        top_k: Annotated[int, "how many closest chunks to return (default 5)"] = 5,
    ) -> str:
        """Search your own memory (past conversations and notes) by semantic similarity.
        It embeds your query into a vector and returns the chunks whose embeddings are
        closest, so matching is by meaning, not exact words. For best results query with
        the key concepts/entities (keywords or a short noun phrase), not a full
        conversational sentence or question. E.g. use 'user-a home city favorite color'
        instead of 'where does the user live and what is their favorite color?'.
        Run several focused queries rather than one long one when you need different facts."""
        hits = agent.query_memory(query, top_k)
        if not hits:
            return "no matching memories."
        return "\n".join(f"[{h['address']}] {h['document']}" for h in hits)

    @tool
    def list_memories(
        source: Annotated[str, "source name; omit to list all sources"] = "",
        path: Annotated[str, "folder inside the source"] = "",
    ) -> str:
        """List the available sources, or the files and folders inside a given source and path,
        so you can discover what memories exist before reading them."""
        entries = sources.list(source, path)
        return "\n".join(entries) if entries else "(empty)"

    @tool
    def read(
        memory_path: Annotated[str, "the 'source:path' address to read (e.g. 'mind_palace:notes/pets.md')"],
        lines: Annotated[Optional[list[int]], "optional [start, end] 1-based line range to return"] = None,
    ) -> str:
        """Retrieve the content of a specific memory by its 'source:path' address.
        Optionally limit the result to a [start, end] line range for large files."""
        name = memory_path.split(":", 1)[0]
        agent.index.sync_source(name)  # reindex-on-read: catch external edits
        content = sources.read(memory_path)
        if lines:
            rows = content.splitlines()
            start = max(1, lines[0])
            end = lines[1] if len(lines) > 1 else start
            content = "\n".join(rows[start - 1 : end])
        return content

    @tool
    def write(
        memory_path: Annotated[str, "the 'source:path' address to write"],
        content: Annotated[str, "the full file contents"],
    ) -> str:
        """Create a new memory or overwrite an existing one at the given 'source:path' address.
        Use edit or append for partial changes to avoid rewriting the whole file."""
        path = sources.write(memory_path, content)
        agent.index.sync_source(memory_path.split(":", 1)[0])  # reindex-on-edit
        return f"wrote {path}"

    @tool
    def append(
        memory_path: Annotated[str, "the 'source:path' address to append to"],
        content: Annotated[str, "the text to add at the end of the file"],
    ) -> str:
        """Add content to the end of a memory without rewriting it (creates it if missing).
        Prefer this over write when you only want to add a note, not replace the file."""
        source, rel = sources.resolve(memory_path)
        existing = source.read(rel) if source.exists(rel) else ""
        joined = content if not existing else existing + ("" if existing.endswith("\n") else "\n") + content
        path = source.write(rel, joined)
        agent.index.sync_source(source.name)
        return f"appended to {path}"

    @tool
    def edit(
        memory_path: Annotated[str, "the 'source:path' address to edit"],
        old_string: Annotated[str, "the exact fragment to replace; must be unique in the file (add surrounding context)"],
        new_string: Annotated[str, "the text to replace it with"],
    ) -> str:
        """Edit part of an existing memory by replacing a specific fragment, without rewriting
        the whole file. old_string must match exactly and appear exactly once."""
        source, rel = sources.resolve(memory_path)
        content = source.read(rel)
        count = content.count(old_string)
        if count == 0:
            return f"error: old_string not found in {memory_path}"
        if count > 1:
            return f"error: old_string is not unique in {memory_path} ({count} matches); add surrounding context"
        source.write(rel, content.replace(old_string, new_string, 1))
        agent.index.sync_source(source.name)
        return f"edited {memory_path}"

    @tool
    def move(
        memory_path: Annotated[str, "the current 'source:path' address"],
        new_path: Annotated[str, "the new 'source:path' address"],
    ) -> str:
        """Move or rename a memory from one address to another. Use it to reorganize your mind palace."""
        source, rel = sources.resolve(memory_path)
        content = source.read(rel)
        dst = sources.write(new_path, content)
        source.delete(rel)
        agent.index.sync_source(source.name)
        agent.index.sync_source(new_path.split(":", 1)[0])
        return f"moved {memory_path} -> {dst}"

    @tool
    def delete(memory_path: Annotated[str, "the 'source:path' address to delete"]) -> str:
        """Delete a memory at the given address. From your point of view it stops existing."""
        sources.delete(memory_path)
        agent.index.sync_source(memory_path.split(":", 1)[0])
        return f"deleted {memory_path}"

    funcs = [query_memory, list_memories, read, write, append, edit, move, delete]
    specs = [f.tool_spec for f in funcs]
    handlers = {f.tool_spec["function"]["name"]: f for f in funcs}
    return specs, handlers
