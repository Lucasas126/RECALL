from __future__ import annotations

import re
from datetime import datetime
from dataclasses import dataclass, field

from src.config.models import MemoryLimits
from src.config.settings import memory_limits
from src.core.sources.agent_files import SkillInfo, join_memory
from src.core.sources.registry import SourceRegistry

QUESTIONS_FILE = "questions.md"
MAX_PROMPT_QUESTIONS = 3
_QUESTION_RE = re.compile(r"^[-*]\s+(?!\[[xX]\])(?:\[ \]\s+)?(.+\S)\s*$")


@dataclass(frozen=True)
class AgentProfile:
    """Snapshot of the agent's own files taken when a chat (or dream) builds its system prompt."""

    soul: str = ""
    memory: list[str] = field(default_factory=list)
    user: list[str] = field(default_factory=list)
    skills: list[SkillInfo] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    limits: MemoryLimits = field(default_factory=memory_limits)
    loaded_at: str = ""  # ISO time of the read; empty for a hand-built profile (no loaded line in the prompt)


def open_questions(text: str, limit: int = MAX_PROMPT_QUESTIONS) -> list[str]:
    """Open top-level list items of `questions.md`; items checked as `- [x]` count as answered."""
    found = []
    for line in text.splitlines():
        match = _QUESTION_RE.match(line)
        if match:
            found.append(match.group(1))
            if len(found) >= limit:
                break
    return found


def load_profile(sources: SourceRegistry, *, recall_knowledge: bool = True) -> AgentProfile:
    """Read SOUL, memories, skill index and open questions; a missing SOUL.md is an empty personality."""
    files = sources.agent_files()
    palace = sources.mind_palace()
    questions = palace.read(QUESTIONS_FILE) if palace.exists(QUESTIONS_FILE) else ""
    return AgentProfile(
        soul=files.soul_text(),
        memory=files.memory_entries("memory"),
        user=files.memory_entries("user"),
        skills=files.skills(include_builtin=recall_knowledge),
        questions=open_questions(questions),
        limits=memory_limits(),
        loaded_at=datetime.now().astimezone().isoformat(timespec="seconds"),
    )


def profile_blocks(profile: AgentProfile) -> list[str]:
    """System-prompt blocks after SOUL: memories, skills index and open questions (empty ones are left out)."""
    blocks = []
    if profile.memory:
        text = join_memory(profile.memory)
        blocks.append(
            f"## Your memory ({len(text)}/{profile.limits.memory_chars} chars)\n\n"
            f"Your own durable notes, kept with the `memory` tool (target `memory`).\n\n{text}"
        )
    if profile.user:
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
    if profile.questions:
        items = "\n".join(f"- {question}" for question in profile.questions)
        blocks.append(
            "## Questions you want to ask the user\n\n"
            "Ask at most one, and only when it fits naturally in the conversation; never interrupt a task "
            "for it. When the user answers, remove or annotate the question in `mind_palace:questions.md` "
            f"(if you can edit it) and save what you learned.\n\n{items}"
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
