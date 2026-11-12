"""Zero data retention (ZDR) is an optional per-user policy, off unless the user asks for it.

`require_zdr` in the user's yaml is the default of every ZDR-aware feature. A feature's own `zdr` setting (or a
chat's stored flag) overrides it; None means "inherit". ZDR is an OpenRouter routing filter: it never applies to
Claude subscription models, which run under the user's own Anthropic account settings.
"""
from __future__ import annotations

from typing import Optional

from src.config.model_refs import is_claude_subscription


def zdr_required(require_zdr: bool, override: Optional[bool] = None) -> bool:
    """Whether ZDR applies: the feature's own setting when it has one, else the user's."""
    return require_zdr if override is None else override


def chat_zdr(require_zdr: bool, model: str, stored: Optional[bool] = None) -> bool:
    """A chat's effective ZDR flag: always off for Claude subscription models, else its stored flag or the user's."""
    return not is_claude_subscription(model) and zdr_required(require_zdr, stored)
