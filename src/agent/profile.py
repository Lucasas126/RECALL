from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass, field

from src.config.models import MemoryLimits
from src.config.settings import memory_limits
from src.config.spaces import scaled_user_chars
from src.core.sources.agent_files import SkillInfo, join_memory
from src.core.sources.registry import SourceRegistry

FOR_USER_FILE = "for_user.md"      # questions, ideas and findings the agent wants to bring up with the user
DREAM_TASKS_FILE = "dream_tasks.md"  # work the user asked for the next dream (research, self-improvements)
MAX_PROMPT_FOR_USER = 3
MAX_DREAM_TASKS = 20
_ITEM_RE = re.compile(r"^[-*]\s+(?!\[[xX]\])(?:\[ \]\s+)?(.*\S)\s*$")


@dataclass(frozen=True)
class AgentProfile:
    """Snapshot of the agent's own files taken when a chat (or dream) builds its system prompt."""

    soul: str = ""
    memory: list[str] = field(default_factory=list)
    user: list[str] = field(default_factory=list)
    skills: list[SkillInfo] = field(default_factory=list)
    for_user: list[str] = field(default_factory=list)
    limits: MemoryLimits = field(default_factory=memory_limits)
    user_text: str = ""  # space agents: USER.md as written (sections per member); `user` is then empty
    user_names: dict[str, str] = field(default_factory=dict)  # space agents: member id -> display name
    user_chars: int | None = None  # space agents: the scaled USER.md limit; None = `limits.user_chars`
    loaded_at: str = ""  # ISO time of the read; empty for a hand-built profile (no loaded line in the prompt)


def open_items(text: str, limit: int = MAX_PROMPT_FOR_USER) -> list[str]:
    """Open top-level list items of a palace list (`for_user.md`, `dream_tasks.md`); `- [x]` items are done."""
    found = []
    for line in text.splitlines():
        match = _ITEM_RE.match(line)
        if match:
            found.append(match.group(1))
            if len(found) >= limit:
                break
    return found


def load_profile(
    sources: SourceRegistry, *, recall_knowledge: bool = True, space_members: dict[str, str] | None = None,
) -> AgentProfile:
    """Read SOUL, memories, skill index and the open `for_user.md` items; a missing SOUL.md is an empty personality."""
    files = sources.agent_files()
    palace = sources.mind_palace()
    for_user = palace.read(FOR_USER_FILE) if palace.exists(FOR_USER_FILE) else ""
    shared = {}
    if space_members:  # a space-owned agent: USER.md has one section per member and a bigger limit
        shared = {
            "user_text": files.memory_text("user"), "user_names": dict(space_members),
            "user_chars": scaled_user_chars(memory_limits().user_chars, len(space_members)),
        }
    return AgentProfile(
        soul=files.soul_text(),
        memory=files.memory_entries("memory"),
        user=[] if shared else files.memory_entries("user"),
        **shared,
        skills=files.skills(include_builtin=recall_knowledge),
        for_user=open_items(for_user),
        limits=memory_limits(),
        loaded_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def profile_blocks(profile: AgentProfile) -> list[str]:
    """System-prompt blocks after SOUL: memories, skills index and open `for_user.md` items (empty ones are left out)."""
    blocks = []
    if profile.memory:
        text = join_memory(profile.memory)
        blocks.append(
            f"## Your memory ({len(text)}/{profile.limits.memory_chars} chars)\n\n"
            f"Your own durable notes, kept with the `memory` tool (target `memory`).\n\n{text}"
        )
    if profile.user_names:
        from src.agent.user_sections import prompt_text

        text = prompt_text(profile.user_text, profile.user_names)
        if text:
            blocks.append(
                f"## About the users ({len(profile.user_text)}/{profile.user_chars} chars)\n\n"
                "What you know about each member of this shared space, kept with the `memory` tool (target `user`). "
                "The tool changes only the section of the person speaking.\n\n" + text
            )
    elif profile.user:
        text = join_memory(profile.user)
        blocks.append(
            f"## About the user ({len(text)}/{profile.limits.user_chars} chars)\n\n"
            f"What you know about the user, kept with the `memory` tool (target `user`).\n\n{text}"
        )
    builtin = [skill for skill in profile.skills if skill.builtin]
    own = [skill for skill in profile.skills if not skill.builtin]
    if builtin or own:
        parts = ["## Skills"]
        if builtin:
            index = "\n".join(f"- {skill.name}: {skill.description}" for skill in builtin)
            parts.append(
                "Built in (read-only), shipped with RECALL and always matching the running version. Load it with "
                "`skill_view(name)` and its references with `skill_view(name, file_path)`; you cannot edit it, "
                f"so keep your own notes in a skill of your own.\n\n{index}"
            )
        if own:
            index = "\n".join(f"- {skill.name}: {skill.description}" for skill in own)
            parts.append(
                "Procedures you wrote for yourself. When a task matches one, load it with "
                f"`skill_view(name)` before acting and follow it.\n\n{index}"
            )
        blocks.append("\n\n".join(parts))
    if profile.for_user:
        items = "\n".join(f"- {item}" for item in profile.for_user)
        blocks.append(
            "## For the user\n\n"
            "Questions, ideas and findings you want to bring up with the user. Raise at most one per chat, and "
            "only when it fits naturally; never interrupt a task for it. Once you said it or the user answered, "
            "remove the item from `mind_palace:for_user.md` (if you can edit it) and save what you learned."
            f"\n\n{items}"
        )
    if profile.loaded_at:
        recall = " + built-in recall" if builtin else ""
        blocks.append(
            f"Profile loaded {profile.loaded_at}; SOUL {len(profile.soul)} chars, {len(own)} skills (own){recall}"
        )
    return blocks


def system_prompt(profile: AgentProfile, extra_blocks: list[str] | None = None, permissions: str = "") -> str:
    """SOUL first, then the `permissions` block (empty = none), the profile blocks and channel-specific blocks."""
    parts = [profile.soul.strip(), permissions.strip(), *profile_blocks(profile), *(extra_blocks or [])]
    return "\n\n".join(part for part in parts if part)
