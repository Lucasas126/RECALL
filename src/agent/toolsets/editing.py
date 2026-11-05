from __future__ import annotations

from contextlib import nullcontext
from typing import TYPE_CHECKING, Annotated, Callable

from src.agent.tooling import tool
from src.agent.toolsets.common import mark_written, readonly_error

if TYPE_CHECKING:
    from src.agent.agent import Agent


def build_edit_tools(agent: Agent) -> list[Callable[..., str]]:
    """Build tools that create, modify, move or delete memories."""
    sources = agent.sources

    def locked():
        """The agent's per-folder write lock (the one the self memory uses), so a task of the agent and a normal
        chat of it, in this or in another process, cannot interleave their read-modify-write cycles."""
        try:
            return sources.agent_files().memory_lock()
        except (KeyError, AttributeError):  # a registry without the agent source (nothing shared to protect)
            return nullcontext()

    @tool
    def write(
        memory_path: Annotated[str, "the 'source:path' address to write"],
        content: Annotated[str, "the full file contents"],
    ) -> str:
        """Create a new memory or overwrite an existing one at the given 'source:path' address.
        Use edit or append for partial changes to avoid rewriting the whole file."""
        if error := readonly_error(sources, memory_path):
            return error
        with locked():
            path = sources.write(memory_path, content)
        mark_written(agent, memory_path)
        return f"wrote {path}"

    @tool
    def append(
        memory_path: Annotated[str, "the 'source:path' address to append to"],
        content: Annotated[str, "the text to add at the end of the file"],
    ) -> str:
        """Add content to the end of a memory without rewriting it (creates it if missing).
        Prefer this over write when you only want to add a note, not replace the file."""
        if error := readonly_error(sources, memory_path):
            return error
        source, rel = sources.resolve(memory_path)
        with locked():
            existing = source.read(rel) if source.exists(rel) else ""
            joined = content if not existing else existing + ("" if existing.endswith("\n") else "\n") + content
            path = source.write(rel, joined)
        mark_written(agent, memory_path)
        return f"appended to {path}"

    @tool
    def edit(
        memory_path: Annotated[str, "the 'source:path' address to edit"],
        old_string: Annotated[str, "the exact fragment to replace; must be unique in the file (add surrounding context)"],
        new_string: Annotated[str, "the text to replace it with"],
    ) -> str:
        """Edit part of an existing memory by replacing a specific fragment, without rewriting
        the whole file. old_string must match exactly and appear exactly once."""
        if error := readonly_error(sources, memory_path):
            return error
        source, rel = sources.resolve(memory_path)
        with locked():
            content = source.read(rel)
            count = content.count(old_string)
            if count == 0:
                return f"error: old_string not found in {memory_path}"
            if count > 1:
                return f"error: old_string is not unique in {memory_path} ({count} matches); add surrounding context"
            source.write(rel, content.replace(old_string, new_string, 1))
        mark_written(agent, memory_path)
        return f"edited {memory_path}"

    @tool
    def move(
        memory_path: Annotated[str, "the current 'source:path' address"],
        new_path: Annotated[str, "the new 'source:path' address"],
    ) -> str:
        """Move or rename a memory from one address to another. Use it to reorganize your mind palace."""
        if error := readonly_error(sources, memory_path) or readonly_error(sources, new_path):
            return error
        source, rel = sources.resolve(memory_path)
        with locked():
            content = source.read(rel)
            destination = sources.write(new_path, content)
            source.delete(rel)
        mark_written(agent, memory_path)
        mark_written(agent, new_path)
        return f"moved {memory_path} -> {destination}"

    @tool
    def delete(memory_path: Annotated[str, "the 'source:path' address to delete"]) -> str:
        """Delete a memory at the given address. From your point of view it stops existing."""
        if error := readonly_error(sources, memory_path):
            return error
        with locked():
            sources.delete(memory_path)
        mark_written(agent, memory_path)
        return f"deleted {memory_path}"

    return [write, append, edit, move, delete]
