"""Zero data retention (ZDR) is an optional per-user policy, off unless the user asks for it.

`require_zdr` in the user's yaml is the default of every ZDR-aware feature. A feature's own `zdr` setting (or a
chat's stored flag) overrides it; None means "inherit". Claude subscription models have no zero-retention routing.
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


def ensure_zdr_compatible(model: Optional[str], zdr: Optional[bool], section: str) -> None:
    """Refuse a Claude subscription model where ZDR is required (it has no zero-retention routing)."""
    if zdr and is_claude_subscription(model):
        raise ValueError(
            f"{section}: Claude subscription models cannot be used while zero data retention is required "
            f"(require_zdr in user.yaml or {section}.zdr); set {section}.zdr: false to use {model}"
        )
