from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Callable, Optional

from src.agent.tooling import tool
from src.agent.toolsets.common import readable_sources
from src.config.settings import INDEX_FILE_TYPES

if TYPE_CHECKING:
    from src.agent.agent import Agent

_MAX_SEARCH_HITS = 50


def build_memory_tools(agent: Agent) -> list[Callable[..., str]]:
    """Build semantic search, text search, listing and reading tools."""
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
        # the current chat is already in context: searching it would only echo the conversation back
        hits = agent.query_memory(query, top_k, exclude_chat=getattr(agent, "chat_id", None))
        if not hits:
            return "no matching memories."
        return "\n\n".join(
            f"[{hit['address']}{' > ' + hit['context'] if hit.get('context') else ''}] (score {hit['score']:.2f})\n"
            f"{hit.get('body', hit['document'])}"
            for hit in hits
        )

    @tool
    def search(
        query: Annotated[str, "text to find (case-insensitive, literal match)"],
        source: Annotated[str, "source name; omit to search all sources"] = "",
    ) -> str:
        """Find exact text matches across your memories (complements query_memory, which is semantic).
        Returns each match as 'source:path:line'. Use it when you know the wording you're looking for."""
        needle = query.lower()
        hits = []
        for src in readable_sources(sources, source):
            for rel in src.walk(INDEX_FILE_TYPES):
                try:
                    text = src.read(rel)
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"[{src.name}:{rel}:{number}] {line.strip()}")
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
    def read(
        memory_path: Annotated[str, "the 'source:path' address to read (e.g. 'mind_palace:notes/pets.md')"],
        lines: Annotated[Optional[list[int]], "optional [start, end] 1-based line range to return"] = None,
    ) -> str:
        """Retrieve the content of a specific memory by its 'source:path' address.
        Optionally limit the result to a [start, end] line range for large files."""
        content = sources.read(memory_path)
        if lines:
            rows = content.splitlines()
            start = max(1, lines[0])
            end = lines[1] if len(lines) > 1 else start
            content = "\n".join(rows[start - 1 : end])
        return content

    return [query_memory, search, list_memories, read]
