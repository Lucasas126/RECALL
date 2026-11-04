"""Model references: a bare model id served by the user's main provider, or `<provider>:<model>`.

Only the known provider names below count as a prefix, so model ids that contain a colon themselves
(Ollama's `llama3:8b`, OpenRouter's `vendor/model:free`) stay bare ids.
"""
from __future__ import annotations

from typing import Literal

OPENROUTER = "openrouter"
OLLAMA = "ollama"
CLAUDE_SUBSCRIPTION = "claude_subscription"
PROVIDERS: tuple[str, ...] = (OPENROUTER, OLLAMA, CLAUDE_SUBSCRIPTION)
# providers that may be the user's main provider (they also serve embeddings)
MAIN_PROVIDERS: tuple[str, ...] = (OPENROUTER, OLLAMA)
CLAUDE_SUBSCRIPTION_PREFIX = f"{CLAUDE_SUBSCRIPTION}:"

Capability = Literal["chat", "stt", "embeddings"]
CAPABILITIES: dict[str, frozenset[str]] = {
    OPENROUTER: frozenset({"chat", "stt", "embeddings"}),
    OLLAMA: frozenset({"chat", "embeddings"}),
    CLAUDE_SUBSCRIPTION: frozenset({"chat"}),
}
LABELS = {OPENROUTER: "OpenRouter", OLLAMA: "Ollama", CLAUDE_SUBSCRIPTION: "Claude subscription"}


def prefix_of(model: str | None) -> str | None:
    """The provider named by a `<provider>:` prefix, or None for a bare id."""
    if not isinstance(model, str):
        return None
    head, sep, _ = model.partition(":")
    return head if sep and head in PROVIDERS else None


def split_reference(model: str, main: str) -> tuple[str, str]:
    """`(provider, model id)` of a reference; bare ids belong to the main provider."""
    provider = prefix_of(model)
    return (provider, model[len(provider) + 1:]) if provider else (main, model)


def provider_of(model: str, default: str) -> str:
    """The provider that serves a model reference; bare ids belong to `default`."""
    return split_reference(model, default)[0]


def model_name(model: str) -> str:
    """The provider's own model id: the reference without a known provider prefix."""
    provider = prefix_of(model)
    return model[len(provider) + 1:] if provider else model


def reference(provider: str, model: str, main: str) -> str:
    """The reference for a provider's model: bare for the main provider, prefixed otherwise."""
    return model if provider == main else f"{provider}:{model}"


def is_claude_subscription(model: str | None) -> bool:
    """Whether a model reference names the experimental Claude subscription provider."""
    return prefix_of(model) == CLAUDE_SUBSCRIPTION


def claude_model(model: str) -> str:
    """The Claude Code model name of a `claude_subscription:<model>` reference (bare names pass through)."""
    return model.removeprefix(CLAUDE_SUBSCRIPTION_PREFIX)


def validate_reference(model: str) -> str:
    """Reject a `<provider>:` reference without a model name."""
    provider = prefix_of(model)
    if provider and not model_name(model).strip():
        raise ValueError(f"model reference {model!r} needs a model name after '{provider}:'")
    return model
