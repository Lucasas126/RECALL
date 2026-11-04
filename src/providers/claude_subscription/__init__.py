"""Experimental Claude subscription provider: the official Claude Code CLI with the user's own login.

Adapted from the MIT-licensed Hermes plugin "claude-subscription-directsdk-experimental"
(Copyright (c) 2026 Nous Research and contributors). See docs/user-guide/claude-subscription.md.
"""
from __future__ import annotations

from src.config.models import ClaudeSubscriptionConfig
from src.providers.claude_subscription.native import (
    ClaudeCodeLoggedOut, ClaudeCodeMissing, ClaudeCodeTooOld, ClaudeSubscriptionError,
)
from src.providers.claude_subscription.provider import ClaudeSubscriptionProvider

__all__ = [
    "ClaudeCodeLoggedOut", "ClaudeCodeMissing", "ClaudeCodeTooOld", "ClaudeSubscriptionError",
    "ClaudeSubscriptionProvider", "claude_subscription_provider",
]


def claude_subscription_provider(settings: ClaudeSubscriptionConfig, *, purpose: str = "this model") -> ClaudeSubscriptionProvider:
    """The provider for one user's Claude Code login.

    Whether the CLI is installed and logged in is checked when it runs: `ClaudeCodeMissing` and
    `ClaudeCodeLoggedOut` (both `ProviderUnavailable`) carry the actionable fix.
    """
    return ClaudeSubscriptionProvider(
        command=[settings.command] if settings.command else None,
        config_dir=settings.claude_config_dir,
        telemetry=settings.claude_code_telemetry,
        timeout_seconds=settings.timeout_seconds,
    )
