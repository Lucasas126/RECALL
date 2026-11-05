"""Choose the provider instance that serves a model reference for one user's provider settings."""
from __future__ import annotations

import os

from src.config.model_refs import CLAUDE_SUBSCRIPTION, LABELS, OLLAMA, OPENROUTER, split_reference
from src.config.models import ClaudeSubscriptionConfig, OllamaConfig, OpenRouterConfig
from src.config.secrets import get_secret
from src.providers.claude_subscription import claude_subscription_provider
from src.providers.provider import LLMProvider, ProviderUnavailable, get_provider


def provider_for(
    name: str, *, main: str, main_api_key: str, openrouter: OpenRouterConfig, ollama: OllamaConfig,
    claude: ClaudeSubscriptionConfig, user_id: str | None = None, purpose: str = "this model",
) -> LLMProvider:
    """The provider `name` with the user's settings; raises ProviderUnavailable when it cannot be used.

    Any provider can be chosen for a model; whether it works is decided here, at call time: OpenRouter needs
    its key variable set, Claude Code reports a missing install or login when it runs, Ollama when it is down.
    """
    if name == CLAUDE_SUBSCRIPTION:
        return claude_subscription_provider(claude, user_id=user_id, purpose=purpose)
    settings = {OPENROUTER: openrouter, OLLAMA: ollama}.get(name)
    if settings is None:
        raise ValueError(f"unknown provider {name!r} for {purpose}")
    if name == OPENROUTER:
        key = main_api_key if name == main else get_secret(openrouter.api_key).strip()
        if not key:
            raise ProviderUnavailable(
                f"provider not available: {LABELS[name]}: API key variable '{openrouter.api_key}' is not set "
                f"(needed by {purpose})"
            )
        return get_provider(OPENROUTER, api_key=key)
    return get_provider(OLLAMA, api_key=main_api_key if name == main else "", base_url=ollama.host or "")


def provider_for_reference(model: str, *, purpose: str = "this model", **settings) -> LLMProvider:
    """The provider serving `model`: its `<provider>:` prefix, else the main provider."""
    name, _ = split_reference(model, settings["main"])
    return provider_for(name, purpose=purpose, **settings)
