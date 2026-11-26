"""Spaces: pseudo-users that own agents shared by several members (docs/project/design-shared-agents.md).

A space is a `user.yaml` with `kind: space`. This module holds what needs more than one user file: the derived
`max_level`, `spaces_of`, `resolve_owner` and the agent-id rule across a user's own agents and their spaces.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

import yaml

from src.config.levels import Level
from src.config.models import SPACE_ID_PATTERN, UserConfig
from src.config.yaml_files import load_yaml_file

_SPACE_ID = re.compile(SPACE_ID_PATTERN)
_USER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SpaceError(ValueError):
    """A space cannot be resolved (a member is missing or invalid)."""


def is_space_id(value: str) -> bool:
    return bool(_SPACE_ID.match(value))


def new_space_id(config_dir: Path) -> str:
    """A fresh `space-<8 hex>` id that has no folder yet."""
    while True:
        candidate = f"space-{secrets.token_hex(4)}"
        if not (config_dir / candidate).exists():
            return candidate


def _read(config_dir: Path, user_id: str) -> UserConfig | None:
    """The raw config of `user_id` (spaces without their derived level), or None when it is not a readable user."""
    if not _USER_ID.match(user_id):
        return None
    path = config_dir / user_id / "user.yaml"
    try:
        return UserConfig.model_validate(load_yaml_file(path))
    except (OSError, ValueError, yaml.YAMLError):  # includes pydantic.ValidationError and a missing file
        return None


def derived_max_level(space: UserConfig, config_dir: Path) -> Level:
    """The lowest `max_level` among the members of `space`; raises SpaceError when a member cannot be read.

    Fail closed: a missing or invalid member never raises the ceiling, it makes the space unusable.
    """
    levels = []
    for member in space.members:
        user = _read(config_dir, member)
        if user is None or user.kind != "user":
            raise SpaceError(f"space '{space.id}': member '{member}' is not a readable user")
        levels.append(user.max_level)
    return min(levels)


def resolve_space(user: UserConfig, config_dir: Path) -> UserConfig:
    """`user` unchanged, or for a space the same config with `max_level` derived from its members."""
    if user.kind != "space":
        return user
    return user.model_copy(update={"max_level": derived_max_level(user, config_dir)})


def all_spaces(config_dir: Path) -> list[UserConfig]:
    """Every readable space (derived level not applied), sorted by id."""
    found = []
    if config_dir.is_dir():
        for folder in sorted(config_dir.iterdir()):
            if is_space_id(folder.name) and (folder / "user.yaml").is_file():
                space = _read(config_dir, folder.name)
                if space is not None and space.kind == "space":
                    found.append(space)
    return found


def spaces_of(user_id: str, config_dir: Path) -> list[str]:
    """Ids of the spaces `user_id` is a member of."""
    return [space.id for space in all_spaces(config_dir) if user_id in space.members]


def resolve_owner(user_id: str, agent_id: str, config_dir: Path) -> str | None:
    """The owner id holding `agent_id` for `user_id`: the user itself, else the space of theirs that has it, else None.

    A user's own agent wins. Ids never clash across the user and their spaces (`agent_id_conflict`), so at most one
    space matches for a consistent configuration; if two do, the first by id is returned.
    """
    own = _read(config_dir, user_id)
    if own is None or own.kind != "user":
        return None
    if agent_id in own.agents:
        return user_id
    for space in all_spaces(config_dir):
        if user_id in space.members and agent_id in space.agents:
            return space.id
    return None


def agent_id_conflict(user_id: str, agent_id: str, config_dir: Path, *, ignore: str | None = None) -> str | None:
    """The owner (user or space of `user_id`) that already has `agent_id`, or None. `ignore` skips one owner."""
    own = _read(config_dir, user_id)
    if own is not None and user_id != ignore and agent_id in own.agents:
        return user_id
    for space in all_spaces(config_dir):
        if user_id in space.members and space.id != ignore and agent_id in space.agents:
            return space.id
    return None


def members_conflict(space: UserConfig, new_member: str, config_dir: Path) -> str | None:
    """Describe an agent id clash that adding `new_member` to `space` would create, or None.

    Checks the space's agents against the member's own agents and against the agents of their other spaces.
    """
    for agent_id in space.agents:
        owner = agent_id_conflict(new_member, agent_id, config_dir, ignore=space.id)
        if owner is not None:
            return f"agent '{agent_id}' of space '{space.id}' clashes with an agent of '{owner}'"
    return None
