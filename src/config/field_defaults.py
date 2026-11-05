"""Defaults and bounds the interface shows next to each setting, read from the pydantic models.

Nothing here is typed by hand: a field's default and its ge/le/gt/lt bounds are taken from the model itself,
so the forms cannot drift from what the backend validates.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from src.config.models import AgentDelegationConfig, AgentTerminalConfig, ChatConfig, DreamConfig, SubagentSettings


def field_spec(model: type[BaseModel], name: str) -> dict[str, Any]:
    """`{"default", "min", "max"}` of one field; `min`/`max` are null when the model sets no bound."""
    info = model.model_fields[name]
    default = None if info.default is PydanticUndefined else info.default
    bounds: dict[str, Any] = {}
    for item in info.metadata:
        for attribute, key in (("ge", "min"), ("gt", "min"), ("le", "max"), ("lt", "max")):
            if hasattr(item, attribute):
                bounds[key] = getattr(item, attribute)
    return {"default": default, "min": bounds.get("min"), "max": bounds.get("max")}


def _specs(model: type[BaseModel], names: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    return {name: field_spec(model, name) for name in names}


def agent_defaults() -> dict[str, dict[str, dict[str, Any]]]:
    """Defaults and bounds of the agent form, grouped like the agent YAML: chat_config, dream, terminal, delegation."""
    return {
        "chat_config": _specs(ChatConfig, (
            "stream", "has_memory", "reasoning", "reasoning_effort", "reasoning_tokens", "keep_reasoning_in_memory",
            "conversation_messages_retained", "tools", "associative_memory", "associative_execution",
            "model", "recall_knowledge",
        )),
        "dream": _specs(DreamConfig, (
            "enabled", "every_hours", "daily_at", "model", "reasoning_effort", "max_steps", "max_tokens",
            "max_minutes", "notify_telegram",
        )),
        "terminal": _specs(AgentTerminalConfig, ("timeout_seconds", "max_output_chars", "allow_in_dream")),
        "delegation": _specs(AgentDelegationConfig, ("enabled", "max_parallel", "timeout_seconds", "max_tokens")),
        "subagents": _specs(SubagentSettings, ("daily_token_budget",)),
    }
