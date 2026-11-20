"""Defaults and bounds the interface shows next to each setting, read from the pydantic models.

Nothing here is typed by hand: a field's default and its ge/le/gt/lt bounds are taken from the model itself,
so the forms cannot drift from what the backend validates.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from src.config.models import AgentDelegationConfig, SubagentSettings, AgentTerminalConfig, ChatConfig, CompactionConfig, DreamConfig


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


def agent_defaults(max_tool_hops: int | None = None, subagents: SubagentSettings | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    """Defaults and bounds of the agent form, grouped like the agent YAML: chat_config, dream, terminal, delegation.

    Fields that are empty in the model because they follow settings.yaml (`max_tool_hops`, the delegation limits) get the
    effective application value as their default when `max_tool_hops` / `subagents` are given (the loaded settings).
    """
    defaults = {
        "chat_config": _specs(ChatConfig, (
            "stream", "has_memory", "reasoning", "reasoning_effort", "reasoning_tokens", "keep_reasoning_in_memory",
            "max_tool_hops", "tools", "associative_memory", "associative_execution",
            "model", "recall_knowledge",
        )),
        "dream": _specs(DreamConfig, (
            "enabled", "every_hours", "daily_at", "model", "reasoning_effort", "max_tokens",
            "max_minutes", "notify_telegram", "delegate", "delegate_model",
        )),
        "compaction": _specs(CompactionConfig, ("enabled", "mode", "trigger", "keep_recent", "model")),
        "terminal": _specs(AgentTerminalConfig, ("timeout_seconds", "max_output_chars")),
        "delegation": _specs(AgentDelegationConfig, ("enabled", "max_parallel", "timeout_seconds", "max_tokens")),
    }
    if max_tool_hops is not None:
        defaults["chat_config"]["max_tool_hops"]["default"] = max_tool_hops
    if subagents is not None:
        for name, value in (("max_parallel", subagents.max_parallel_per_parent), ("timeout_seconds", subagents.timeout_seconds),
                            ("max_tokens", subagents.max_tokens)):
            defaults["delegation"][name]["default"] = value
    return defaults
