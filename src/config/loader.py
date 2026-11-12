from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

from src.config.agents import AgentStore
from src.config.levels import check_agent_level, effective_agent_level
from src.config.models import AgentConfig, ModelsConfig, ResolvedAgent, UserConfig
from src.config.preferences import UserPreferencesStore
from src.config.secrets import get_secret
from src.config.settings import ASSOC, CHAT_NAMING, CONFIG_DIR, DATA_DIR, MCP, TERMINAL
from src.agent.tool_groups import DEFAULT_TOOL_GROUPS
from src.terminal.resolve import resolve_backends
from src.utils.i18n import InputError
from src.utils.logger import get_logger

logger = get_logger("config")


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


def user_models(
    user: UserConfig, preferences: UserPreferencesStore, agent: Optional[AgentConfig] = None,
) -> ModelsConfig:
    """Resolve inference overrides while preserving the user's embedding model.

    The chat model is the agent's default when `agent` sets one, else the user's default. A model chosen
    inside a chat is applied later, per chat.
    """
    saved = preferences.get(user.id)
    chat = saved.default_llm_model or user.models.chat
    if agent is not None:
        chat = agent.chat_config.model or chat
    return user.models.model_copy(update={
        "chat": chat, "stt": saved.default_stt_model or user.models.stt,
        "stt_language": saved.stt_language or user.models.stt_language,
    })


def load_user_models(user_id: str, agent: Optional[AgentConfig] = None) -> ModelsConfig:
    return user_models(load_user(user_id), UserPreferencesStore(CONFIG_DIR), agent)


def load_agent(user_id: str, agent_id: Optional[str] = None) -> ResolvedAgent:
    """Load a user and one of its agents, resolving the provider key from the environment.

    `models.chat` is the agent's default chat model when it sets one, else the user's.
    """

    user = load_user(user_id)

    agent_id = agent_id or user.agents[0]
    if agent_id not in user.agents:
        raise ValueError(f"agent '{agent_id}' is not owned by user '{user_id}'")

    agent = load_agent_config(user_id, agent_id)
    max_level = user.max_level
    check_agent_level(agent, user, max_level)  # required level <= effective level <= the owner's max_level, else invalid
    level = effective_agent_level(agent, user)

    # local providers (e.g. ollama) need no key; only resolve one if configured
    api_key = ""
    if user.provider.api_key:
        api_key = get_secret(user.provider.api_key)
        if not api_key:
            raise ValueError(f"env var '{user.provider.api_key}' is not set")

    # embeddings provider falls back to the chat provider when not configured
    embeddings_provider = user.embeddings_provider or user.provider
    embeddings_api_key = api_key if embeddings_provider is user.provider else ""
    if embeddings_provider is not user.provider and embeddings_provider.api_key:
        embeddings_api_key = get_secret(embeddings_provider.api_key)
        if not embeddings_api_key:
            raise ValueError(f"env var '{embeddings_provider.api_key}' is not set")

    selected_tool_groups = agent.chat_config.tool_groups
    if selected_tool_groups is None and agent.chat_config.tools:
        selected_tool_groups = list(DEFAULT_TOOL_GROUPS)
    unknown_mcp = sorted(set(agent.chat_config.mcp) - set(MCP.servers))
    if unknown_mcp:
        raise ValueError(f"agent '{agent.id}' enables unknown MCP servers: {', '.join(unknown_mcp)}")
    terminal_backends = {}
    if "terminal" in (selected_tool_groups or ()):
        try:  # an unusable terminal block never breaks the agent: its tools are simply not built
            terminal_backends = resolve_backends(user, agent.terminal, agent_id=agent.id, allow_local=TERMINAL.allow_local)
        except InputError as exc:
            logger.warning("agent %s/%s: terminal tools disabled: %s", user.id, agent.id, exc)
    return ResolvedAgent(
        user_id=user.id,
        language=user.language,
        require_zdr=user.require_zdr,
        id=agent.id,
        name=agent.name,
        models=user_models(user, UserPreferencesStore(CONFIG_DIR), agent),
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
        max_tool_hops=agent.chat_config.max_tool_hops,
        compaction=agent.chat_config.compaction,
        tools=agent.chat_config.tools,
        tool_groups=selected_tool_groups,
        allow=agent.chat_config.allow,
        deny=agent.chat_config.deny,
        mcp=agent.chat_config.mcp,
        associative_memory=agent.chat_config.associative_memory,
        associative_execution=agent.chat_config.associative_execution,
        recall_knowledge=agent.chat_config.recall_knowledge,
        dream=agent.dream,
        folders=agent.folders,
        terminal=agent.terminal,
        delegation=agent.delegation,
        terminal_backends=terminal_backends,
        claude_subscription=user.claude_subscription,
        openrouter=user.openrouter_settings(),
        ollama=user.ollama,
        level=level,
        max_level=max_level,
        user_approval_rules=user.approval_rules,
        approval_rules=agent.approval_rules,
    )
