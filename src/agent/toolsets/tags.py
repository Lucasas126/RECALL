from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, Callable

import yaml

from src.agent.tooling import tool
from src.agent.toolsets.common import readable_sources

if TYPE_CHECKING:
    from src.agent.agent import Agent

_TAG_RE = re.compile(r"(?<!\w)#([A-Za-z0-9_][\w/-]*)")


def _note_tags(text: str) -> set[str]:
    """Extract tags from a note's YAML frontmatter and inline text."""
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
                    tags.update(str(item) for item in value)
            except yaml.YAMLError:
                pass
            body = text[end + 4:]
    tags.update(match.group(1) for match in _TAG_RE.finditer(body))
    return {tag.lstrip("#").strip() for tag in tags if tag and tag.strip()}


def build_tag_tools(agent: Agent) -> list[Callable[..., str]]:
    """Build tools for listing and finding tagged notes."""
    sources = agent.sources

    @tool
    def list_tags(source: Annotated[str, "source name; omit to scan all sources"] = "") -> str:
        """List the tags used across your notes (frontmatter and inline #tags), so you can navigate
        by topic. Your mind palace is obsidian-like, so tag your notes to make this useful."""
        counts: dict[str, int] = {}
        for src in readable_sources(sources, source):
            for rel in src.walk({"md"}):
                try:
                    tags = _note_tags(src.read(rel))
                except OSError:
                    continue
                for tag in tags:
                    counts[tag] = counts.get(tag, 0) + 1
        if not counts:
            return "no tags."
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return "\n".join(f"#{tag} ({count})" for tag, count in ordered)

    @tool
    def find_notes_by_tag(
        tags: Annotated[list[str], "tags to match (with or without the leading #)"],
        source: Annotated[str, "source name; omit to scan all sources"] = "",
        mode: Annotated[str, "'any' (at least one tag) or 'all' (every tag); default 'any'"] = "any",
    ) -> str:
        """Find the notes tagged with the given tags. mode 'any' returns notes with at least one of
        the tags; mode 'all' returns only notes that have all of them."""
        wanted = {tag.lstrip("#").strip() for tag in tags if tag.strip()}
        if not wanted:
            return "no tags given."
        matches = []
        for src in readable_sources(sources, source):
            for rel in src.walk({"md"}):
                try:
                    note_tags = _note_tags(src.read(rel))
                except OSError:
                    continue
                matched = wanted <= note_tags if mode == "all" else bool(wanted & note_tags)
                if matched:
                    matches.append(f"{src.name}:{rel}")
        return "\n".join(matches) if matches else "no notes found."

    return [list_tags, find_notes_by_tag]
