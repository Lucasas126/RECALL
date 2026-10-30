from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from src.config.models import AgentConfig, ResolvedAgent, UserConfig
from src.config.settings import CONFIG_DIR


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_user(user_id: str) -> UserConfig:
    """Load a user's config; raises FileNotFoundError if the user does not exist."""
    return UserConfig(**_load_yaml(CONFIG_DIR / user_id / "user.yaml"))


def load_agent_config(user_id: str, agent_id: str) -> AgentConfig:
    """Load one agent's own config (no provider resolution)."""
    return AgentConfig(**_load_yaml(CONFIG_DIR / user_id / "agents" / f"{agent_id}.yaml"))


def load_agent(user_id: str, agent_id: Optional[str] = None) -> ResolvedAgent:
    """Load a user and one of its agents, resolving the provider key from the environment."""

    user = load_user(user_id)

    agent_id = agent_id or user.agents[0]
    if agent_id not in user.agents:
        raise ValueError(f"agent '{agent_id}' is not owned by user '{user_id}'")

    agent = load_agent_config(user_id, agent_id)

    # local providers (e.g. ollama) need no key; only resolve one if configured
    api_key = ""
    if user.provider.api_key:
        api_key = os.environ.get(user.provider.api_key, "")
        if not api_key:
            raise ValueError(f"env var '{user.provider.api_key}' is not set")

    # embeddings provider falls back to the chat provider when not configured
    embeddings_provider = user.embeddings_provider or user.provider
    embeddings_api_key = api_key if embeddings_provider is user.provider else ""
    if embeddings_provider is not user.provider and embeddings_provider.api_key:
        embeddings_api_key = os.environ.get(embeddings_provider.api_key, "")
        if not embeddings_api_key:
            raise ValueError(f"env var '{embeddings_provider.api_key}' is not set")

    return ResolvedAgent(
        user_id=user.id,
        id=agent.id,
        name=agent.name,
        personality_prompt=agent.personality_prompt,
        models=agent.models,
        provider_name=user.provider.name,
        api_key=api_key,
        embeddings_provider_name=embeddings_provider.name,
        embeddings_api_key=embeddings_api_key,
        stream=agent.chat_config.stream,
        has_memory=agent.chat_config.has_memory,
        reasoning=agent.chat_config.reasoning,
        reasoning_effort=agent.chat_config.reasoning_effort,
        reasoning_tokens=agent.chat_config.reasoning_tokens,
        keep_reasoning_in_memory=agent.chat_config.keep_reasoning_in_memory,
        conversation_messages_retained=agent.chat_config.conversation_messages_retained,
        tools=agent.chat_config.tools,
        associative_memory=agent.chat_config.associative_memory,
    )
