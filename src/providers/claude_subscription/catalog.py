# Adapted from the MIT-licensed Hermes plugin "claude-subscription-directsdk-experimental"
# (Copyright (c) 2026 Nous Research and contributors). See docs/user-guide/claude-subscription.md.
"""Pinned Claude Code model routes: aliases, context windows and the CLI version each one needs."""
from __future__ import annotations

from dataclasses import dataclass

from src.config.model_refs import CLAUDE_SUBSCRIPTION_PREFIX, claude_model


@dataclass(frozen=True)
class ClaudeModel:
    alias: str
    canonical: str
    name: str
    context_window: int
    min_cli: tuple[int, int, int]


# aliases shown in the model pickers; Claude Code resolves the alias to the model id
MODELS = (
    ClaudeModel("haiku", "claude-haiku-5-5", "Claude Haiku 5.5", 1_000_000, (2, 1, 293)),
    ClaudeModel("sonnet", "claude-sonnet-5-5", "Claude Sonnet 5.5", 1_000_000, (2, 1, 284)),
    ClaudeModel("opus", "claude-opus-5-5", "Claude Opus 5.5", 1_000_000, (2, 1, 280)),
)
_BY_NAME = {name: model for model in MODELS for name in (model.alias, model.canonical)}
# routes outside the pinned list; their 200K window needs no [1m] suffix
_CONTEXT_WINDOWS = {"claude-haiku-4-5-20251001": 200_000, "claude-haiku-4-5": 200_000}
# models that reject `thinking: disabled`; the lowest effort is the nearest "off" for them
_MANDATORY_THINKING = ("claude-fable", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")


def lookup(model: str) -> ClaudeModel | None:
    """The pinned entry for an alias or canonical id, with or without the reference prefix."""
    return _BY_NAME.get(claude_model(model).removesuffix("[1m]"))


def native_model(model: str) -> str:
    """The `--model` value for Claude Code: pinned 1M routes get the `[1m]` suffix, others pass through."""
    name = claude_model(model)
    entry = lookup(name)
    if entry is not None and entry.context_window == 1_000_000:
        return entry.canonical + "[1m]"
    return name


def context_window(model: str) -> int | None:
    entry = lookup(model)
    if entry is not None:
        return entry.context_window
    return _CONTEXT_WINDOWS.get(claude_model(model))


def thinking_is_mandatory(model: str) -> bool:
    entry = lookup(model)
    return (entry.canonical if entry else claude_model(model)).startswith(_MANDATORY_THINKING)


def parse_version(text: str) -> tuple[int, int, int] | None:
    """`2.1.316 (Claude Code)` -> (2, 1, 316); None when the text carries no version."""
    head = text.strip().split(" ", 1)[0]
    parts = head.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def catalog_entries(query: str = "") -> list[dict]:
    """Picker rows for the pinned subscription models, shaped like OpenRouter catalog rows."""
    query = query.strip().lower()
    rows = []
    for model in MODELS:
        ref = CLAUDE_SUBSCRIPTION_PREFIX + model.alias
        if query and query not in ref.lower() and query not in model.name.lower():
            continue
        rows.append({
            "id": ref,
            "name": f"{model.name} (Claude subscription, experimental)",
            "description": "Runs through your own Claude Code login. Experimental.",
            "zdr_providers": [],
            "pricing": {"prompt": None, "completion": None},
            "input_modalities": ["text", "image"],
            "parameter_routes": [["tools", "reasoning", "reasoning_effort"]],
            "provider": "claude_subscription",
            "experimental": True,
        })
    return rows
