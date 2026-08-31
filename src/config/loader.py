from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from src.config.models import AgentConfig, ResolvedAgent, UserConfig

CONFIG_DIR = Path(".config")


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_agent(user_id: str, agent_id: Optional[str] = None) -> ResolvedAgent:
    """Load a user and one of its agents, resolving the provider key from the environment."""

    user = UserConfig(**_load_yaml(CONFIG_DIR / user_id / "user.yaml"))

    agent_id = agent_id or user.agents[0]
    if agent_id not in user.agents:
        raise ValueError(f"agent '{agent_id}' is not owned by user '{user_id}'")

    agent = AgentConfig(**_load_yaml(CONFIG_DIR / user_id / "agents" / f"{agent_id}.yaml"))

    # local providers (e.g. ollama) need no key; only resolve one if configured
    api_key = ""
    if user.provider.api_key:
        api_key = os.environ.get(user.provider.api_key, "")
        if not api_key:
            raise ValueError(f"env var '{user.provider.api_key}' is not set")

    return ResolvedAgent(
        user_id=user.id,
        id=agent.id,
        name=agent.name,
        personality_prompt=agent.personality_prompt,
        models=agent.models,
        provider_name=user.provider.name,
        api_key=api_key,
        stream=agent.chat_config.stream,
        has_memory=agent.chat_config.has_memory,
        reasoning=agent.chat_config.reasoning,
        reasoning_tokens=agent.chat_config.reasoning_tokens,
        keep_reasoning_in_memory=agent.chat_config.keep_reasoning_in_memory,
    )
