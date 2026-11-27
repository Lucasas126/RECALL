"""Delete a user for good: configuration, data, shared agents and invitations (CLI `delete-user`, web admin screen).

Files only: the busy check is a callback (the web service knows the running replies, the CLI does not). Secrets are
never edited: the result names what the administrator must remove by hand (`leftovers`, names only, never values).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Callable

import yaml

from src.admin.users import AdminError, check_user_id
from src.config.levels import Level
from src.config.models import UserConfig
from src.config.secrets import get_secret, read_secrets
from src.config.settings import CONFIG_DIR, DATA_DIR, TELEGRAM_ALLOWED_USERS_ENV
from src.config.spaces import all_spaces, clear_spaces_cache, is_space_id
from src.config.yaml_files import load_yaml_file
from src.providers.telegram_notify import parse_allowed_users

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEYS = ("api_key", "embeddings_api_key")


def _persons(config_dir: Path) -> dict[str, UserConfig]:
    people: dict[str, UserConfig] = {}
    for path in sorted(config_dir.glob("*/user.yaml")):
        try:
            user = UserConfig.model_validate(load_yaml_file(path))
        except Exception:  # a broken file is not a known administrator
            continue
        if user.kind == "user":
            people[user.id] = user
    return people


def _inside(root: Path, target: Path) -> bool:
    return target.parent == root.resolve() and not target.is_symlink()


def _referenced_names(node, found: set[str]) -> None:
    """Variable NAMES a yaml tree points at (`api_key: NAME`, `*_env: NAME`)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and _ENV_NAME.match(value) and (key in _SECRET_KEYS or str(key).endswith("_env")):
                found.add(value)
            else:
                _referenced_names(value, found)
    elif isinstance(node, list):
        for item in node:
            _referenced_names(item, found)


def _names_in_folder(folder: Path) -> set[str]:
    found: set[str] = set()
    for path in [folder / "user.yaml", *sorted((folder / "agents").glob("*.yaml"))]:
        try:
            _referenced_names(yaml.safe_load(path.read_text(encoding="utf-8")), found)
        except (OSError, yaml.YAMLError):
            continue
    return found


def leftovers(user_id: str, config_dir: Path) -> dict:
    """Secrets the user referenced that nobody else does, and whether Telegram still maps to them. Names only."""
    mine = _names_in_folder(config_dir / user_id)
    from src.config import user_keys

    own = user_keys.variable_for(user_id)
    if own in read_secrets():
        mine.add(own)
    others: set[str] = set()
    for folder in config_dir.glob("*/user.yaml"):
        if folder.parent.name != user_id:
            others |= _names_in_folder(folder.parent)
    allowed, _invalid = parse_allowed_users(get_secret(TELEGRAM_ALLOWED_USERS_ENV))
    return {"secrets": sorted(mine - others), "telegram": user_id in allowed.values(), "telegram_variable": TELEGRAM_ALLOWED_USERS_ENV}


def _check(user_id: str, confirm: str, actor: str | None, config_dir: Path) -> UserConfig:
    check_user_id(user_id)
    if is_space_id(user_id):
        raise AdminError(f"'{user_id}' is a space, not a user (spaces disappear with their last agent).")
    if confirm != user_id:
        raise AdminError("the confirmation must be the user id, typed exactly.")
    if actor is not None and actor == user_id:
        raise AdminError("you cannot delete yourself; ask another administrator.")
    people = _persons(config_dir)
    user = people.get(user_id)
    if user is None:
        raise AdminError(f"'{user_id}' does not exist.")
    if user.max_level == Level.ADMIN and not any(p.max_level == Level.ADMIN for i, p in people.items() if i != user_id):
        raise AdminError("this is the last administrator; make another user an administrator first.")
    return user


def delete_user(
    user_id: str, *, confirm: str, actor: str | None = None, config_dir: Path | None = None, data_dir: Path | None = None,
    is_busy: Callable[[str, str], bool] = lambda owner, agent: False, create_space: Callable | None = None,
    on_removed: Callable[[str, str], None] = lambda owner, agent: None,
    on_created: Callable[[str, str], None] = lambda owner, agent: None,
) -> dict:
    """Delete `user_id`; returns what was done plus `leftovers`. Nothing is removed when a check fails."""
    config_dir, data_dir = Path(config_dir or CONFIG_DIR), Path(data_dir or DATA_DIR)  # read at call time
    user = _check(user_id, confirm, actor, config_dir)
    from src.admin.spaces import space_create
    from src.service.agent_move import AgentMover, MoveError

    spaces = [s for s in all_spaces(config_dir) if user_id in s.members]
    busy = [(user_id, a) for a in user.agents] + [(s.id, a) for s in spaces for a in s.agents]
    for owner, agent in busy:
        if is_busy(owner, agent):
            raise AdminError(f"'{agent}' has a reply, task or process running: wait for it before deleting '{user_id}'.")
    config_target, data_target = (config_dir / user_id).resolve(), (data_dir / user_id).resolve()
    for root, target in ((config_dir, config_target), (data_dir, data_target)):
        if target.exists() and not _inside(root, target):
            raise AdminError(f"refusing to delete {target}: it is not a folder directly inside {root}.")
    leftover = leftovers(user_id, config_dir)  # read before anything is removed
    mover = AgentMover(config_dir, data_dir, create_space=create_space or space_create, is_busy=is_busy,
                       on_removed=on_removed, on_created=on_created)
    shared_kept, shared_removed = [], []
    try:
        for space in spaces:
            remaining = [m for m in space.members if m != user_id]
            for agent in list(space.agents):
                if remaining:
                    shared_kept.append((agent, mover.regroup_agent(space.id, agent, remaining, user_id)))
                else:
                    shared_removed.append(agent)
            if not remaining or not space.agents:
                shutil.rmtree(config_dir / space.id, ignore_errors=True)
                shutil.rmtree(data_dir / space.id, ignore_errors=True)
    except MoveError as exc:
        raise AdminError(f"a shared agent could not move to the remaining members: {exc}") from exc
    clear_spaces_cache()
    invitations = 0
    for path in sorted((data_dir / ".invitations").glob("*.json")) if (data_dir / ".invitations").is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if user_id in (record.get("inviter"), record.get("invitee")):
            path.unlink(missing_ok=True)
            invitations += 1
    for owner, agent in busy:
        on_removed(owner, agent)
    # the destructive part, last; rmtree never follows a symlink out of the folder
    shutil.rmtree(config_target, ignore_errors=False)
    shutil.rmtree(data_target, ignore_errors=True)
    clear_spaces_cache()
    return {"user": user_id, "agents": list(user.agents), "shared_kept": shared_kept, "shared_removed": shared_removed,
            "invitations": invitations, "leftovers": leftover}


def describe(result: dict) -> list[str]:
    lines = [f"Deleted user '{result['user']}': configuration and data folders, agents: {', '.join(result['agents']) or '-'}."]
    for agent, space in result["shared_kept"]:
        lines.append(f"Shared agent '{agent}' stays with the other members ({space}).")
    for agent in result["shared_removed"]:
        lines.append(f"Shared agent '{agent}' had no other member: deleted with the user.")
    lines.append(f"Invitations removed: {result['invitations']}.")
    left = result["leftovers"]
    if left["secrets"]:
        lines.append("Remove by hand (set-secret / secrets file): " + ", ".join(left["secrets"]))
    if left["telegram"]:
        lines.append(f"Remove the '=<user>' entry of the user from {left['telegram_variable']} by hand.")
    return lines


def run(args) -> int:
    import sys

    try:
        if not args.yes:
            raise AdminError("refusing to delete without --yes and --confirm <user id>.")
        print("warning: this works on the files; the CLI cannot see running replies, tasks or processes. Stop them "
              "first and restart the server afterwards (it keeps users in memory).", file=sys.stderr)
        for line in describe(delete_user(check_user_id(args.user), confirm=args.confirm)):
            print(line)
    except (AdminError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
