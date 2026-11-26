"""USER.md of a space-owned agent: one `## <member id>` section per member (docs/project/design-shared-agents.md).

A private agent never goes through this module: its USER.md stays a flat list of `§` entries.
"""

from __future__ import annotations

import re

from src.agent.toolsets.self_files import MemoryOperationError, apply_memory_operations
from src.config.models import ResolvedAgent
from src.config.settings import memory_limits
from src.config.spaces import scaled_user_chars
from src.core.sources.agent_files import join_memory, split_memory

_HEADING = re.compile(r"^## ([A-Za-z0-9_-]{1,64})[ \t]*$", re.MULTILINE)


def user_limit(config: ResolvedAgent) -> int:
    """USER.md char limit: the normal one, times the members of a space (capped at 5x)."""
    base = memory_limits().user_chars
    members = getattr(config, "space_members", None)
    return scaled_user_chars(base, len(members)) if members else base


def parse_sections(text: str) -> dict[str, list[str]]:
    """Member id -> entries of its section. Text before the first heading is dropped (it belongs to nobody)."""
    text = text.replace("\r\n", "\n")
    found = list(_HEADING.finditer(text))
    sections: dict[str, list[str]] = {}
    for index, match in enumerate(found):
        end = found[index + 1].start() if index + 1 < len(found) else len(text)
        sections.setdefault(match.group(1), []).extend(split_memory(text[match.end():end]))
    return sections


def render_sections(sections: dict[str, list[str]]) -> str:
    """The file text: every heading is kept, also an empty one."""
    return "\n\n".join(f"## {member}\n" + join_memory(entries) if entries else f"## {member}\n"
                       for member, entries in sections.items()).rstrip("\n") + ("\n" if sections else "")


def apply_user_operations(
    text: str, operations: list[dict], members: list[str], speaker: str | None,
) -> tuple[str, int]:
    """Apply memory operations to the USER.md `text` of a space agent; returns (new text, entries in the touched scope).

    With a `speaker` only their section can change (it is created on the first add). Without one (a dream) every
    section may change: `add` names its section with a `section` key, `patch` and `remove` find the one entry that
    holds `old_text`. Headings are never merged or dropped.
    """
    sections = parse_sections(text)
    if speaker is not None:
        if speaker not in members:
            raise MemoryOperationError(f"{speaker!r} is not a member of this space")
        sections[speaker] = apply_memory_operations(sections.get(speaker, []), operations)
        return render_sections(sections), len(sections[speaker])
    for number, operation in enumerate(operations, 1):
        label = f"operation {number}"
        if not isinstance(operation, dict):
            raise MemoryOperationError(f"{label}: expected an object with action, content and old_text")
        if str(operation.get("action") or "").strip() == "add":
            section = str(operation.get("section") or "")
            if section not in members:
                raise MemoryOperationError(f"{label}: add needs `section` = one of the member ids {', '.join(members)}")
            sections[section] = apply_memory_operations(sections.get(section, []), [operation])
            continue
        old_text = str(operation.get("old_text") or "")
        owners = [member for member, entries in sections.items() if old_text and any(old_text in entry for entry in entries)]
        if len(owners) > 1:
            raise MemoryOperationError(f"{label}: old_text matches entries of several sections; quote more of the entry")
        if owners:
            sections[owners[0]] = apply_memory_operations(sections[owners[0]], [operation])
        else:
            apply_memory_operations([], [operation])  # raises the standard error for this operation
    return render_sections(sections), sum(len(entries) for entries in sections.values())


def prompt_text(text: str, names: dict[str, str]) -> str:
    """The sections for the system prompt, each headed with the member's display name."""
    parts = []
    for member, entries in parse_sections(text).items():
        if entries:
            parts.append(f"## {names.get(member, member)} ({member})\n" + join_memory(entries))
    return "\n\n".join(parts)
