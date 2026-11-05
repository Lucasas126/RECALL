from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Callable, Optional

from src.agent.tooling import tool
from src.agent.toolsets.common import readable_sources
from src.config.settings import INDEX_FILE_TYPES

if TYPE_CHECKING:
    from src.agent.agent import Agent

_MAX_SEARCH_HITS = 50
_MAX_SEARCH_LIMIT = 200
_COUNT_CAP = 20_000  # stop counting here: the total is reported as '20000+'


def _path_matcher(path: str):
    """A predicate over 'source/relative/path' for a prefix (`skills/github/`) or a glob (`mind_palace/notes/**/*.md`)."""
    path = path.strip().replace("\\", "/").lstrip("/")
    if not path:
        return lambda _full, _rel: True
    if not re.search(r"[*?\[]", path):
        return lambda full, rel: full.startswith(path) or rel.startswith(path)
    out, i = [], 0
    while i < len(path):
        if path.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif path.startswith("**", i):
            out.append(".*")
            i += 2
        elif path[i] == "*":
            out.append("[^/]*")
            i += 1
        elif path[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(path[i]))
            i += 1
    pattern = re.compile("".join(out) + r"\Z")
    return lambda full, rel: bool(pattern.match(full) or pattern.match(rel))


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
        path: Annotated[str, "only files under this prefix or glob, e.g. skills/github/ or mind_palace/**/*.md"] = "",
        offset: Annotated[int, "matches to skip (paging)"] = 0,
        limit: Annotated[int, "matches to return (default 50, max 200)"] = _MAX_SEARCH_HITS,
    ) -> str:
        """Find exact text matches across your memories (complements query_memory, which is semantic).
        Returns each match as 'source:path:line' and says how many exist and how to get the next page."""
        needle = query.lower()
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or _MAX_SEARCH_HITS), _MAX_SEARCH_LIMIT))
        wanted = _path_matcher(path)
        hits: list[str] = []
        total = 0
        for src in readable_sources(sources, source):
            for rel in src.walk(INDEX_FILE_TYPES):
                if not wanted(f"{src.name}/{rel}", rel):
                    continue
                try:
                    text = src.read(rel)
                except OSError:
                    continue
                for number, line in enumerate(text.splitlines(), 1):
                    if needle in line.lower():
                        if offset <= total < offset + limit:
                            hits.append(f"[{src.name}:{rel}:{number}] {line.strip()}")
                        total += 1
                if total >= _COUNT_CAP:
                    break
        if not total:
            return "no matches."
        if not hits:
            return f"no matches at offset {offset}: there are {total} in total."
        count = f"{_COUNT_CAP}+" if total >= _COUNT_CAP else str(total)
        last = offset + len(hits)
        footer = f"showing {offset + 1}-{last} of {count} matches"
        if last < total:
            footer += f"; next page: offset={last}"
        return "\n".join(hits) + f"\n... ({footer})"

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
