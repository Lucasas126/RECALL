"""Agent templates (`python -m src.admin agent create --from-template` and the `agent_create` tool): a config plus a starting SOUL.md."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.agent.default_soul import default_soul
from src.config.levels import Level
from src.config.models import AgentConfig

SOUL_MAX_CHARS = 2000


@dataclass(frozen=True)
class AgentTemplate:
    name: str
    description: str
    config: dict  # AgentConfig fields except id and name
    soul: str
    min_level: Level | None = None  # the lowest `max_level` a user needs for this template
    display_name: str = ""  # name given to the agent when none is chosen (default: the agent id)


def _guide_soul() -> str:
    return Path(__file__).with_name("guide_soul.md").read_text(encoding="utf-8").replace("\r\n", "\n")


def _permission_manager_soul() -> str:
    return Path(__file__).with_name("permission_manager_soul.md").read_text(encoding="utf-8").replace("\r\n", "\n")


def templates() -> dict[str, AgentTemplate]:
    return {
        "default": AgentTemplate("default", "a plain agent with the default SOUL", {}, default_soul()),
        "guide": AgentTemplate(
            "guide",
            "setup guide: explains RECALL and, with your approval, creates and adjusts your agents and their permissions (no terminal)",
            {
                "description": "Guides the user through configuring RECALL; can create and change agents.",
                "level": "operator",
                "chat_config": {"tool_groups": ["memory_read", "memory_edit", "web", "self", "identity", "agents", "permissions"]},
            },
            _guide_soul(),
            min_level=Level.OPERATOR, display_name="Guide",
        ),
        "permission-manager": AgentTemplate(
            "permission-manager",
            "permission manager: explains levels and, with your approval, adjusts the levels, tool groups and approval rules of your agents",
            {
                "description": "Reviews what each of your agents can do and proposes or applies permission changes you approve.",
                "level": "operator",
                "chat_config": {"tool_groups": ["memory_read", "memory_edit", "self", "identity", "permissions"]},
            },
            _permission_manager_soul(),
            min_level=Level.OPERATOR, display_name="Permission manager",
        ),
    }


def build(template: str, agent_id: str, name: str) -> tuple[AgentConfig, str, AgentTemplate]:
    """The validated config and SOUL text of a new agent; raises ValueError for an unknown template."""
    available = templates()
    if template not in available:
        raise ValueError(f"unknown template '{template}'; available: {', '.join(sorted(available))}")
    chosen = available[template]
    return AgentConfig(id=agent_id, name=name, **chosen.config), chosen.soul, chosen
