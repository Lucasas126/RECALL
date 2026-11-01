from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from src.config.agents import AgentStore
from src.config.models import AgentConfig, ModelsConfig, ResolvedAgent, UserConfig
from src.config.preferences import UserPreferencesStore
from src.config.settings import CONFIG_DIR, DATA_DIR, MCP
from src.agent.tool_groups import DEFAULT_TOOL_GROUPS


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_user(user_id: str) -> UserConfig:
    """Load a user's config; raises FileNotFoundError if the user does not exist."""
    return UserConfig(**_load_yaml(CONFIG_DIR / user_id / "user.yaml"))


def load_agent_config(user_id: str, agent_id: str) -> AgentConfig:
    """Load one agent's own config (no provider resolution); raises FileNotFoundError if missing."""
    return AgentStore(CONFIG_DIR, DATA_DIR).load(user_id, agent_id)


def user_models(user: UserConfig, preferences: UserPreferencesStore) -> ModelsConfig:
    """Resolve inference overrides while preserving the user's embedding model."""
    saved = preferences.get(user.id)
    return user.models.model_copy(update={
        "chat": saved.default_llm_model or user.models.chat,
        "stt": saved.default_stt_model or user.models.stt,
    })


def load_user_models(user_id: str) -> ModelsConfig:
    return user_models(load_user(user_id), UserPreferencesStore(CONFIG_DIR))


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

    selected_tool_groups = agent.chat_config.tool_groups
    if selected_tool_groups is None and agent.chat_config.tools:
        selected_tool_groups = list(DEFAULT_TOOL_GROUPS)
    unknown_mcp = sorted(set(agent.chat_config.mcp) - set(MCP.servers))
    if unknown_mcp:
        raise ValueError(f"agent '{agent.id}' enables unknown MCP servers: {', '.join(unknown_mcp)}")
    return ResolvedAgent(
        user_id=user.id,
        language=user.language,
        id=agent.id,
        name=agent.name,
        personality_prompt=agent.personality_prompt,
        models=load_user_models(user_id),
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
        tool_groups=selected_tool_groups,
        allow=agent.chat_config.allow,
        deny=agent.chat_config.deny,
        mcp=agent.chat_config.mcp,
        associative_memory=agent.chat_config.associative_memory,
    )
