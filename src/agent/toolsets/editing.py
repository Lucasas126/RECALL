from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Callable

from src.agent.read_ledger import file_digest
from src.agent.tooling import tool
from src.agent.toolsets.common import ledger_of, mark_written, readonly_error, write_locks

if TYPE_CHECKING:
    from src.agent.agent import Agent


def build_edit_tools(agent: Agent) -> list[Callable[..., str]]:
    """Build tools that create, modify, move or delete memories.

    Every change runs inside the agent's write lock (shared with its other chats, tasks and dreams, in any process),
    so concurrent changes queue instead of interleaving. `write` and `delete` also check the chat's read ledger: a
    file that changed since this chat last read it is refused, never silently overwritten.
    """
    sources = agent.sources
    ledger = ledger_of(agent)

    @tool
    def write(
        memory_path: Annotated[str, "the 'source:path' address to write"],
        content: Annotated[str, "the full file contents"],
    ) -> str:
        """Create a new memory or overwrite an existing one at the given 'source:path' address.
        Overwriting needs a read of the file in this chat, and is refused if the file changed since.
        Use edit or append for partial changes to avoid rewriting the whole file."""
        if error := readonly_error(sources, memory_path):
            return error
        source, rel = sources.resolve(memory_path)
        with write_locks(sources, source):
            path = source.resolve(rel)
            if refusal := ledger.overwrite_refusal(path, memory_path):
                return refusal
            path = source.write(rel, content)
            ledger.saw(path)
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
        with write_locks(sources, source):
            path = source.resolve(rel)
            before = file_digest(path)
            existing = source.read(rel) if source.exists(rel) else ""
            joined = content if not existing else existing + ("" if existing.endswith("\n") else "\n") + content
            path = source.write(rel, joined)
            ledger.changed(path, before)
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
        with write_locks(sources, source):
            path = source.resolve(rel)
            before = file_digest(path)
            content = source.read(rel)
            count = content.count(old_string)
            if count == 0:
                return f"error: old_string not found in {memory_path} (it may have changed since you read it; read it again)"
            if count > 1:
                return f"error: old_string is not unique in {memory_path} ({count} matches); add surrounding context"
            path = source.write(rel, content.replace(old_string, new_string, 1))
            ledger.changed(path, before)
        mark_written(agent, memory_path)
        return f"edited {memory_path}"

    @tool
    def move(
        memory_path: Annotated[str, "the current 'source:path' address"],
        new_path: Annotated[str, "the new 'source:path' address"],
        overwrite: Annotated[bool, "replace the destination when it already exists (default false)"] = False,
    ) -> str:
        """Move or rename a memory from one address to another. Use it to reorganize your mind palace.
        It refuses to replace an existing destination unless overwrite is true."""
        if error := readonly_error(sources, memory_path) or readonly_error(sources, new_path):
            return error
        source, rel = sources.resolve(memory_path)
        target, target_rel = sources.resolve(new_path)
        with write_locks(sources, source, target):
            origin, destination_path = source.resolve(rel), target.resolve(target_rel)
            if origin == destination_path:
                return f"error: {new_path} is the same file as {memory_path}; nothing to move"
            if not origin.is_file():
                return f"error: {memory_path} is not a file"
            if target.exists(target_rel):
                if not overwrite:
                    return f"error: {new_path} already exists; pass overwrite=true to replace it"
                if refusal := ledger.delete_refusal(destination_path, new_path):  # overwrite=true is the consent
                    return refusal.replace("Nothing was deleted", "Nothing was moved")
            known = ledger.get(origin)
            data = source.read_bytes(rel)
            destination = sources.write_bytes(new_path, data)  # bytes: PDFs and images move too
            source.delete(rel)
            ledger.forget(origin)
            ledger.record(destination, known if known == file_digest(destination) else None)
        mark_written(agent, memory_path)
        mark_written(agent, new_path)
        return f"moved {memory_path} -> {destination}"

    @tool
    def delete(memory_path: Annotated[str, "the 'source:path' address to delete"]) -> str:
        """Delete a memory at the given address. From your point of view it stops existing.
        Refused if you read the file in this chat and it changed since."""
        if error := readonly_error(sources, memory_path):
            return error
        source, rel = sources.resolve(memory_path)
        with write_locks(sources, source):
            path = source.resolve(rel)
            if refusal := ledger.delete_refusal(path, memory_path):
                return refusal
            source.delete(rel)
            ledger.forget(path)
        mark_written(agent, memory_path)
        return f"deleted {memory_path}"

    return [write, append, edit, move, delete]
