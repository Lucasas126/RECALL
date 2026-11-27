"""Administrator commands for spaces (agents shared by several users): `python -m src.admin space ...`.

    space list
    space show --space ID
    space create --billing-user U [--member U2 ...] [--name N]
    space add-member --space ID --user U
    space remove-member --space ID --user U [--new-billing-user U2]
    space delete --space ID --yes

A space is a pseudo-user (`kind: space`) with no password: it never logs in. `create` copies the provider, models,
Claude login and terminal-backend settings of the billing user (variable NAMES only, never values). Every change
writes one line to `<data_dir>/<space>/admin_audit.jsonl`. Design: docs/project/design-shared-agents.md.
"""

from __future__ import annotations

import shutil
import sys

import yaml
from pydantic import ValidationError

from src.admin.agents import dump_yaml
from src.admin.users import AdminError, _write_yaml, check_user_id
from src.config.agents import AgentStore
from src.config.levels import parse_level
from src.config.models import UserConfig
from src.config.settings import CONFIG_DIR, DATA_DIR
from src.config.spaces import (
    SpaceError, all_spaces, is_space_id, members_conflict, new_space_id, resolve_space,
)
from src.config.yaml_files import load_yaml_file
from src.terminal.audit import write_audit
from src.utils.files import exclusive_file_lock
from src.config.errors import describe_config_error

AUDIT_FILE = "admin_audit.jsonl"
# settings copied from the billing user: they decide who pays and where the agents may run
COPIED_KEYS = (
    "language", "require_zdr", "provider", "embeddings_provider", "models", "openrouter", "ollama",
    "claude_subscription", "terminal_backends",
)


def _user_file(user_id: str):
    return CONFIG_DIR / check_user_id(user_id) / "user.yaml"


def _read_raw(user_id: str) -> dict:
    path = _user_file(user_id)
    if not path.is_file():
        raise AdminError(f"'{user_id}' does not exist.")
    data = load_yaml_file(path)
    if not isinstance(data, dict):
        raise AdminError(f"{path} is not a valid user.yaml.")
    return data


def _person(user_id: str) -> UserConfig:
    """A loadable real user (never a space)."""
    try:
        user = UserConfig.model_validate(_read_raw(user_id))
    except ValidationError as exc:
        raise AdminError(f"user '{user_id}' is invalid: {describe_config_error(exc)}") from exc
    if user.kind != "user":
        raise AdminError(f"'{user_id}' is a space, not a user.")
    return user


def _space(space_id: str) -> UserConfig:
    if not is_space_id(space_id):
        raise AdminError(f"'{space_id}' is not a space id (space-<8 hex digits>).")
    try:
        space = UserConfig.model_validate(_read_raw(space_id))
    except ValidationError as exc:
        raise AdminError(f"space '{space_id}' is invalid: {describe_config_error(exc)}") from exc
    if space.kind != "space":
        raise AdminError(f"'{space_id}' is not a space.")
    return space


def _audit(space_id: str, action: str, **fields) -> None:
    if not write_audit(DATA_DIR / space_id / AUDIT_FILE, event="admin_action", action=action, actor="cli", space=space_id, **fields):
        print(f"warning: the audit file of {space_id} could not be written", file=sys.stderr)


def _write_checked(space_id: str, data: dict, previous: str | None) -> None:
    """Write `user.yaml` of a space only if it validates and loads; restore `previous` (or remove it) on failure."""
    path = _user_file(space_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _write_yaml(path, data)
        resolve_space(UserConfig.model_validate(load_yaml_file(path)), CONFIG_DIR)
    except (ValidationError, SpaceError, ValueError) as exc:
        if previous is None:
            shutil.rmtree(path.parent, ignore_errors=True)
        else:
            path.write_text(previous, encoding="utf-8")
        raise AdminError(f"the space file would not be valid, nothing was kept: {describe_config_error(exc)}") from exc


def _check_agents_fit(space_id: str, members: list[str], data: dict) -> None:
    """The derived level of the new member set must still cover every agent of the space."""
    candidate = UserConfig.model_validate({**data, "members": members})
    level = resolve_space(candidate, CONFIG_DIR).max_level
    above = [row for row in AgentStore(CONFIG_DIR, DATA_DIR).agent_levels(space_id, candidate)
             if parse_level(row["level"]) > level]
    if above:
        raise AdminError(
            f"the lowest max_level of the members would be {level.label}, below the level of: "
            + ", ".join(f"{row['id']} ({row['level']})" for row in above)
        )


def space_create(billing_user: str, members: list[str], name: str | None) -> str:
    billing = _person(billing_user)
    everyone = [billing_user, *[m for m in members if m != billing_user]]
    for member in everyone[1:]:
        _person(member)
    space_id = new_space_id(CONFIG_DIR)
    raw = _read_raw(billing_user)
    data: dict = {"id": space_id, "name": name or space_id, "kind": "space", "members": everyone,
                  "billing_user": billing_user}
    data.update({key: raw[key] for key in COPIED_KEYS if raw.get(key) is not None})
    data["agents"] = []
    _write_checked(space_id, data, None)
    _audit(space_id, "create", billing_user=billing.id, members=everyone)
    return space_id


def space_add_member(space_id: str, user_id: str) -> None:
    space = _space(space_id)
    _person(user_id)
    if user_id in space.members:
        raise AdminError(f"'{user_id}' is already a member of {space_id}.")
    clash = members_conflict(space, user_id, CONFIG_DIR)
    if clash:
        raise AdminError(f"cannot add '{user_id}': {clash}. Rename one of the agents first.")
    with exclusive_file_lock(CONFIG_DIR / space_id / "agents.lock"):
        previous = _user_file(space_id).read_text(encoding="utf-8")
        data = _read_raw(space_id)
        members = [*space.members, user_id]
        _check_agents_fit(space_id, members, data)
        _write_checked(space_id, {**data, "members": members}, previous)
    _audit(space_id, "add_member", user=user_id)


def space_remove_member(space_id: str, user_id: str, new_billing_user: str | None) -> None:
    space = _space(space_id)
    if user_id not in space.members:
        raise AdminError(f"'{user_id}' is not a member of {space_id}.")
    if len(space.members) == 1:
        raise AdminError(f"'{user_id}' is the last member: delete the space instead.")
    members = [m for m in space.members if m != user_id]
    billing = space.billing_user
    if user_id == space.billing_user:
        if new_billing_user is None:
            raise AdminError(f"'{user_id}' pays for {space_id}: pass --new-billing-user <member>.")
        if new_billing_user not in members:
            raise AdminError("--new-billing-user must be one of the remaining members.")
        billing = new_billing_user
    elif new_billing_user is not None:
        raise AdminError("--new-billing-user is only used when removing the billing user.")
    with exclusive_file_lock(CONFIG_DIR / space_id / "agents.lock"):
        previous = _user_file(space_id).read_text(encoding="utf-8")
        data = _read_raw(space_id)
        data.update({"members": members, "billing_user": billing})
        if billing != space.billing_user:  # the new payer's providers and Claude login replace the old ones
            raw = _read_raw(billing)
            for key in COPIED_KEYS:  # what the new payer lacks must not stay from the old one
                data.pop(key, None)
            data.update({key: raw[key] for key in COPIED_KEYS if raw.get(key) is not None})
        _write_checked(space_id, data, previous)
    _audit(space_id, "remove_member", user=user_id, billing_user=billing)  # nothing is deleted when a member leaves


def space_delete(space_id: str, yes: bool) -> None:
    space = _space(space_id)
    if space.agents:
        raise AdminError(f"{space_id} still has agents ({', '.join(space.agents)}): delete them first "
                         "(`agent delete --user <space> ...`). A space lives while it has agents.")
    if not yes:
        raise AdminError("deleting a space needs --yes (its data folder, if any, is kept).")
    _audit(space_id, "delete", members=space.members)
    shutil.rmtree(CONFIG_DIR / space_id)


def space_list() -> int:
    spaces = all_spaces(CONFIG_DIR)
    if not spaces:
        print("No spaces.")
    for space in spaces:
        try:
            level = resolve_space(space, CONFIG_DIR).max_level.label
        except SpaceError as exc:
            level = f"unusable ({exc})"
        print(f"{space.id}  name={space.name!r}  members={','.join(space.members)}  billing={space.billing_user}  "
              f"max_level={level}  agents={','.join(space.agents) or '-'}")
    return 0


def space_show(space_id: str) -> int:
    space = _space(space_id)
    try:
        space = resolve_space(space, CONFIG_DIR)
    except SpaceError as exc:
        print(f"# unusable: {exc}")
    shown = space.model_dump(mode="json", exclude_none=True, exclude={"password_hash"})
    print(dump_yaml(shown).rstrip())
    print("# max_level is derived from the members; key variables are NAMES only")
    return 0


def add_parsers(sub) -> None:
    space = sub.add_parser("space", help="spaces: agents shared by several users")
    ssub = space.add_subparsers(dest="space_command", required=True)
    ssub.add_parser("list", help="every space with members, payer, derived max_level and agents")
    ssub.add_parser("show", help="user.yaml of a space").add_argument("--space", required=True)
    create = ssub.add_parser("create", help="create a space paid by a user (copies that user's provider settings)")
    create.add_argument("--billing-user", required=True, help="the member whose keys and Claude login the space uses")
    create.add_argument("--member", action="append", default=[], help="another member (repeatable)")
    create.add_argument("--name", help="display name (default: the space id)")
    for command, text in (("add-member", "add a user to a space"), ("remove-member", "remove a user from a space")):
        parser = ssub.add_parser(command, help=text)
        parser.add_argument("--space", required=True)
        parser.add_argument("--user", required=True)
        if command == "remove-member":
            parser.add_argument("--new-billing-user", help="required when the removed member is the payer")
    delete = ssub.add_parser("delete", help="delete a space that has no agents (data is kept)")
    delete.add_argument("--space", required=True)
    delete.add_argument("--yes", action="store_true", help="confirm")


def run(args) -> int:
    try:
        command = args.space_command
        if command == "list":
            return space_list()
        if command == "show":
            return space_show(args.space)
        if command == "create":
            space_id = space_create(args.billing_user, args.member, args.name)
            print(f"Created space {space_id} (members: {args.billing_user}"
                  + "".join(f", {m}" for m in args.member if m != args.billing_user) + ")")
        elif command == "add-member":
            space_add_member(args.space, args.user)
            print(f"Added '{args.user}' to {args.space}.")
        elif command == "remove-member":
            space_remove_member(args.space, args.user, args.new_billing_user)
            print(f"Removed '{args.user}' from {args.space}. Nothing was deleted.")
        else:
            space_delete(args.space, args.yes)
            print(f"Deleted the configuration of {args.space}.")
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (SpaceError, ValidationError, ValueError, OSError, yaml.YAMLError) as exc:
        print(f"error: {describe_config_error(exc)}", file=sys.stderr)
        return 1
    return 0
