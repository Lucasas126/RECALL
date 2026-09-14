from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Callable, Optional

import yaml

from src.agent.tooling import tool
from src.config.settings import INDEX_FILE_TYPES
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.agent import Agent

logger = get_logger("tools")

_MAX_SEARCH_HITS = 50
_TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_][\w/-]*)")


def _note_tags(text: str) -> set[str]:
    """Extract tags from a note: YAML frontmatter ``tags:`` plus inline ``#tags``."""
    tags: set[str] = set()
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(text[3:end]) or {}
                value = meta.get("tags")
                if isinstance(value, str):
                    tags.update(re.split(r"[,\s]+", value.strip()))
                elif isinstance(value, list):
                    tags.update(str(v) for v in value)
            except yaml.YAMLError:
                pass
            body = text[end + 4:]
    tags.update(m.group(1) for m in _TAG_RE.finditer(body))
    return {t.lstrip("#").strip() for t in tags if t and t.strip()}


def build_tools(agent: "Agent") -> tuple[list[dict], dict[str, Callable[..., str]]]:
    """Build the OpenAI-style tool specs and their handlers, bound to *agent*.

    Each tool is a plain typed function; its schema is derived from the signature,
    ``Annotated`` parameter descriptions and docstring (see ``@tool``). Handlers
    return a string (what the model sees back); writes keep the index in sync.
    """
    sources = agent.sources

    def _targets(source: str) -> list:
        """The readable sources to scan: a single one if named, else all of them."""
        names = [source] if source else sources.names()
        return [sources.get(n) for n in names if sources.get(n).readable]

    def _readonly_error(memory_path: str) -> Optional[str]:
        """Return an error message if the target source is not writable by the agent's tools."""
        source, _ = sources.resolve(memory_path)
        if not source.writable:
            return f"error: source '{source.name}' is read-only; you can only write to note sources like mind_palace"
        return None

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
    def search(
        query: Annotated[str, "text to find (case-insensitive, literal match)"],
        source: Annotated[str, "source name; omit to search all sources"] = "",
    ) -> str:
        """Find exact text matches across your memories (complements query_memory, which is semantic).
        Returns each match as 'source:path:line'. Use it when you know the wording you're looking for."""
        needle = query.lower()
        hits = []
        for src in _targets(source):
            for rel in src.walk(INDEX_FILE_TYPES):
                try:
                    text = src.read(rel)
                except OSError:
                    continue
                for n, line in enumerate(text.splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"[{src.name}:{rel}:{n}] {line.strip()}")
                        if len(hits) >= _MAX_SEARCH_HITS:
                            return "\n".join(hits) + f"\n... (stopped at {_MAX_SEARCH_HITS} matches)"
        return "\n".join(hits) if hits else "no matches."

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
    def list_tags(source: Annotated[str, "source name; omit to scan all sources"] = "") -> str:
        """List the tags used across your notes (frontmatter and inline #tags), so you can navigate
        by topic. Your mind palace is obsidian-like, so tag your notes to make this useful."""
        counts: dict[str, int] = {}
        for src in _targets(source):
            for rel in src.walk({"md"}):
                try:
                    tags = _note_tags(src.read(rel))
                except OSError:
                    continue
                for tag in tags:
                    counts[tag] = counts.get(tag, 0) + 1
        if not counts:
            return "no tags."
        return "\n".join(f"#{t} ({c})" for t, c in sorted(counts.items(), key=lambda x: (-x[1], x[0])))

    @tool
    def find_notes_by_tag(
        tags: Annotated[list[str], "tags to match (with or without the leading #)"],
        source: Annotated[str, "source name; omit to scan all sources"] = "",
        mode: Annotated[str, "'any' (at least one tag) or 'all' (every tag); default 'any'"] = "any",
    ) -> str:
        """Find the notes tagged with the given tags. mode 'any' returns notes with at least one of
        the tags; mode 'all' returns only notes that have all of them."""
        want = {t.lstrip("#").strip() for t in tags if t.strip()}
        if not want:
            return "no tags given."
        matches = []
        for src in _targets(source):
            for rel in src.walk({"md"}):
                try:
                    note_tags = _note_tags(src.read(rel))
                except OSError:
                    continue
                matched = want <= note_tags if mode == "all" else bool(want & note_tags)
                if matched:
                    matches.append(f"{src.name}:{rel}")
        return "\n".join(matches) if matches else "no notes found."

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
        if error := _readonly_error(memory_path):
            return error
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
        if error := _readonly_error(memory_path):
            return error
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
        if error := _readonly_error(memory_path):
            return error
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
        if error := (_readonly_error(memory_path) or _readonly_error(new_path)):
            return error
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
        if error := _readonly_error(memory_path):
            return error
        sources.delete(memory_path)
        agent.index.sync_source(memory_path.split(":", 1)[0])
        return f"deleted {memory_path}"

    funcs = [query_memory, search, list_memories, list_tags, find_notes_by_tag, read, write, append, edit, move, delete]
    specs = [f.tool_spec for f in funcs]
    handlers = {f.tool_spec["function"]["name"]: f for f in funcs}
    return specs, handlers
