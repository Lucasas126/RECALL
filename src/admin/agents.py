"""Administrator commands for agents and user settings, through the same service the web UI uses.

    python -m src.admin agent list --user U
    python -m src.admin agent show --user U --agent A
    python -m src.admin agent create --user U --id A [--name N] [--from-template default|guide] [--set path=value ...]
    python -m src.admin agent set --user U --agent A --set path=value [--set ...]
    python -m src.admin agent rename --user U --agent A --new-id B [--name N]
    python -m src.admin agent delete --user U --agent A --yes
    python -m src.admin agent check [--user U]
    python -m src.admin user show --user U
    python -m src.admin user set --user U --set path=value [--set ...]

Values after `=` are parsed as YAML (`true`, `42`, `[a, b]`, `{k: v}`, `null`). Nothing invalid is ever written:
the result is validated with the real models first, errors are printed readably and the files stay untouched.
YAML only holds the NAMES of environment variables, so no command here prints a secret.
"""

from __future__ import annotations

import copy
import re
import sys
import typing
from pathlib import Path

import yaml
from pydantic import ValidationError

from src.admin.users import AdminError, _ENV_NAME_RE, check_user_id
from src.agent import templates as agent_templates
from src.config.agents import AgentConfigError, AgentStore
from src.config.errors import describe_config_error
from src.config.loader import agent_default_model, load_agent_config, load_user
from src.config.models import AgentConfig, UserConfig
from src.config.secrets import is_set
from src.config.settings import CONFIG_DIR, DATA_DIR, TERMINAL
from src.terminal.output import redact
from src.terminal.resolve import resolve_backends
from src.utils.files import atomic_write_text, exclusive_file_lock
from src.utils.i18n import InputError, error_message

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PROTECTED_USER_PATHS = {"password_hash": "use `set-password` (the hash is never shown or set here)",
                         "id": "the user id cannot change", "agents": "use the `agent` commands to change the agent list"}


class _IndentDumper(yaml.SafeDumper):
    """Indents list items under their key, the layout AgentStore's `agents:` list editor expects."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: D401
        return super().increase_indent(flow, False)


def dump_yaml(data: object) -> str:
    return yaml.dump(data, Dumper=_IndentDumper, sort_keys=False, allow_unicode=True, default_flow_style=False)


# ------------------------------------------------------------------ dotted paths


_YAML_HINT = "values are parsed as YAML: wrap text containing \": \" (or starting with [, {, #, *, &) in quotes"


def _field_type(model, parts: list[str]):
    """The annotation of the field at the dotted path in a pydantic model, or None when it cannot be told
    (unknown key, a list, a union that is not just Optional...)."""
    current = model
    for index, key in enumerate(parts):
        while typing.get_origin(current) is typing.Annotated:
            current = typing.get_args(current)[0]
        if isinstance(current, type) and hasattr(current, "model_fields"):
            field = current.model_fields.get(key)
            if field is None:
                return None
            current = field.annotation
        elif typing.get_origin(current) is dict:
            current = typing.get_args(current)[1] if len(typing.get_args(current)) == 2 else None
        else:
            return None
        if current is None:
            return None
    while typing.get_origin(current) is typing.Annotated:
        current = typing.get_args(current)[0]
    return current


def _strip_quotes(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def parse_assignment(item: str, model=None, *, raw_text: bool = False) -> tuple[list[str], object]:
    """`a.b.c=value` -> (['a','b','c'], value). The value is YAML, except for a string field of `model`
    (taken raw, one pair of quotes stripped) and when `raw_text` forces the raw string."""
    path, sep, raw = item.partition("=")
    path = path.strip()
    if not sep or not path:
        raise AdminError(f"invalid --set '{path or item}': use path=value, e.g. chat_config.model=openrouter:vendor/model")
    parts = path.split(".")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
        raise AdminError(f"invalid path '{path}': use dotted keys such as terminal.backends.local.approvals")
    if raw_text or (model is not None and _field_type(model, parts) is str):
        return parts, _strip_quotes(raw)
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AdminError(f"the value of '{path}' is not valid YAML ({describe_config_error(exc)}); {_YAML_HINT}") from exc
    return parts, value


def apply_assignments(data: dict, assignments: list[str], model=None, *, raw_indexes: frozenset[int] = frozenset()) -> dict:
    """A deep copy of `data` with every `path=value` applied (intermediate maps are created)."""
    result = copy.deepcopy(data)
    for number, item in enumerate(assignments):
        parts, value = parse_assignment(item, model, raw_text=number in raw_indexes)
        node = result
        for index, key in enumerate(parts[:-1]):
            child = node.get(key)
            if child is None:
                child = node[key] = {}
            if not isinstance(child, dict):
                raise AdminError(f"cannot set '{'.'.join(parts)}': '{'.'.join(parts[:index + 1])}' is not a map "
                                 "(set the whole list or value at that path instead)")
            node = child
        node[parts[-1]] = value
    return result


def apply_validated(data: dict, assignments: list[str], model, validate):
    """`validate(apply_assignments(...))`; when it fails, retries once with the raw text for the assignments whose YAML
    value is not a string (a value like `a: b` that YAML turned into a map), and uses the first retry that validates."""
    try:
        return validate(apply_assignments(data, assignments, model))
    except AdminError as first:
        candidates = []
        for i, item in enumerate(assignments):
            try:
                if not isinstance(parse_assignment(item, model)[1], str):
                    candidates.append(i)
            except AdminError:
                candidates.append(i)
        for attempt in [frozenset([i]) for i in candidates] + ([frozenset(candidates)] if len(candidates) > 1 else []):
            try:
                return validate(apply_assignments(data, assignments, model, raw_indexes=attempt))
            except AdminError:
                continue
        raise AdminError(f"{first} ({_YAML_HINT})") from None


def _paths(assignments: list[str]) -> list[str]:
    return [".".join(parse_assignment(item)[0]) for item in assignments]


# ------------------------------------------------------------------ service plumbing


def _make_service():
    from src.service.chat import ChatService

    return ChatService(agent_factory=lambda *args, **kwargs: None)


def _user_dir(user_id: str) -> Path:
    return CONFIG_DIR / check_user_id(user_id)


def _store() -> AgentStore:
    return AgentStore(CONFIG_DIR, DATA_DIR)


def _require_user(user_id: str) -> None:
    if not (_user_dir(user_id) / "user.yaml").is_file():
        raise AdminError(f"User '{user_id}' does not exist.")


def _validate_agent(data: dict) -> AgentConfig:
    try:
        return AgentConfig.model_validate(data)
    except ValidationError as exc:
        raise AdminError("the agent would be invalid, nothing was written: " + describe_config_error(exc)) from exc


def _agent_data(service, user_id: str, agent_id: str) -> dict:
    return service.get_agent(user_id, agent_id)


def _clean(config: AgentConfig) -> dict:
    return config.model_dump(mode="json", exclude_none=True)


# ------------------------------------------------------------------ agent commands


def agent_list(service, user_id: str) -> int:
    _require_user(user_id)
    rows = service.list_agent_configs(user_id)
    if not rows:
        print(f"User '{user_id}' has no agents.")
        return 0
    for row in rows:
        chat = row.get("chat_config") or {}
        groups = chat.get("tool_groups")
        print(f"{row['id']}  name={row['name']!r}  model={chat.get('model') or '(user default)'}  "
              f"tool_groups={','.join(groups) if groups is not None else '(defaults)'}")
    return 0


def agent_show(service, user_id: str, agent_id: str) -> int:
    _require_user(user_id)
    config = AgentConfig.model_validate(_agent_data(service, user_id, agent_id))
    print(dump_yaml(_clean(config)).rstrip())
    return 0


def _ensure_guide_backend(user_id: str, name: str, definition: dict) -> bool:
    """Add the user's terminal backend `name` when missing (True when added); refuses to replace another type."""
    path = _user_dir(user_id) / "user.yaml"
    with exclusive_file_lock(_user_dir(user_id) / "agents.lock"):
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        backends = raw.get("terminal_backends") or {}
        existing = backends.get(name)
        if existing is not None:
            if existing.get("type") != definition["type"]:
                raise AdminError(f"user '{user_id}' already defines terminal backend '{name}' with another type; "
                                 f"rename it or pick another template.")
            return False
        raw["terminal_backends"] = {**backends, name: definition}
        _write_validated_user(path, text, raw)
    return True


def _write_validated_user(path: Path, previous: str, raw: dict) -> None:
    try:
        UserConfig.model_validate(raw)
    except ValidationError as exc:
        raise AdminError("user.yaml would be invalid, nothing was written: " + describe_config_error(exc)) from exc
    atomic_write_text(path, dump_yaml(raw))
    try:
        load_user(path.parent.name)
    except Exception as exc:
        atomic_write_text(path, previous)
        raise AdminError("user.yaml would not load, the previous file was restored: " + describe_config_error(exc)) from exc


def agent_create(service, user_id: str, agent_id: str, *, name: str | None, template: str, assignments: list[str]) -> int:
    _require_user(user_id)
    if not _ID_RE.match(agent_id):
        raise AdminError("Invalid agent id: use 1-64 letters, digits, '_' or '-'.")
    display = name or ("Guide" if template == "guide" else agent_id)
    try:
        config, soul, chosen = agent_templates.build(template, agent_id, display)
    except ValueError as exc:
        raise AdminError(str(exc)) from exc
    config = apply_validated(_clean(config), assignments, AgentConfig, _validate_agent)
    previous = (_user_dir(user_id) / "user.yaml").read_text(encoding="utf-8")
    backend_added = False
    if chosen.needs_backend:
        backend_added = _ensure_guide_backend(user_id, agent_templates.GUIDE_BACKEND, agent_templates.GUIDE_BACKEND_DEFINITION)
    try:
        service.create_agent(user_id, config)
        if soul != agent_templates.templates()["default"].soul:
            service.update_soul(user_id, agent_id, soul)
    except Exception:
        if backend_added:  # do not leave the backend behind for an agent that was not created
            with exclusive_file_lock(_user_dir(user_id) / "agents.lock"):
                atomic_write_text(_user_dir(user_id) / "user.yaml", previous)
        raise
    print(f"Created agent '{agent_id}' ({display}) for user '{user_id}' from template '{template}'.")
    if backend_added:
        print(f"Added terminal backend '{agent_templates.GUIDE_BACKEND}' (type local, workdir /app) to user.yaml.")
    if chosen.needs_backend:
        print("Note: the local backend only works when the server allows it (settings.yaml terminal.allow_local or "
              "RECALL_TERMINAL_ALLOW_LOCAL=true in the server's environment); commands run as the app user and "
              "reach every user's config. Read references/security-for-admins.md first.")
    print("It is available on the next message in the web UI and Telegram; no restart needed.")
    return 0


def agent_set(service, user_id: str, agent_id: str, assignments: list[str]) -> int:
    _require_user(user_id)
    if not assignments:
        raise AdminError("give at least one --set path=value")
    for item in assignments:
        if parse_assignment(item)[0][0] == "id":
            raise AdminError("the id cannot be changed with `agent set`; use `agent rename`.")
    current = _agent_data(service, user_id, agent_id)
    updated = apply_validated(current, assignments, AgentConfig, _validate_agent)
    service.update_agent(user_id, agent_id, updated)
    print(f"Updated agent '{agent_id}': {', '.join(_paths(assignments))}. Applies on the next message.")
    return 0


def agent_rename(service, user_id: str, agent_id: str, new_id: str, name: str | None) -> int:
    _require_user(user_id)
    if not _ID_RE.match(new_id):
        raise AdminError("Invalid new agent id: use 1-64 letters, digits, '_' or '-'.")
    data = _agent_data(service, user_id, agent_id)
    data["id"] = new_id
    if name:
        data["name"] = name
    updated = _validate_agent(data)
    service.update_agent(user_id, agent_id, updated)
    print(f"Renamed agent '{agent_id}' to '{new_id}' (data folder data/{user_id}/{new_id}, preferences updated). "
          "Chats keep their history. Applies on the next message.")
    return 0


def agent_delete(service, user_id: str, agent_id: str, *, yes: bool) -> int:
    _require_user(user_id)
    if not yes:
        raise AdminError("refusing to delete without --yes (this removes the agent's configuration; "
                         "its data folder is kept on disk).")
    service.delete_agent(user_id, agent_id)
    print(f"Deleted agent '{agent_id}' of '{user_id}'. Its data folder data/{user_id}/{agent_id} was kept.")
    return 0


# ------------------------------------------------------------------ check


def _user_ids() -> list[str]:
    if not CONFIG_DIR.is_dir():
        return []
    return [d.name for d in sorted(CONFIG_DIR.iterdir()) if _ID_RE.match(d.name) and (d / "user.yaml").is_file()]


def check_user(user_id: str) -> tuple[list[str], list[str], int]:
    """(errors, warnings, agent count) for one user, loading everything through the real loader."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        user = load_user(user_id)
    except Exception as exc:
        return [f"{user_id}/user.yaml: {describe_config_error(exc)}"], warnings, 0
    store = _store()
    agents_dir = CONFIG_DIR / user_id / "agents"
    listed = set(user.agents)
    if len(listed) != len(user.agents):
        errors.append(f"{user_id}/user.yaml: the agents list repeats an id")
    seen: set[str] = set()
    for path in sorted(agents_dir.glob("*.yaml")) if agents_dir.is_dir() else []:
        try:
            config = store._read(path)
        except Exception as exc:
            errors.append(f"{user_id}/agents/{path.name}: {describe_config_error(exc)}")
            continue
        if config.id in seen:
            errors.append(f"{user_id}/agents/{path.name}: duplicate agent id '{config.id}'")
        seen.add(config.id)
        if config.id not in listed:
            warnings.append(f"{user_id}/agents/{path.name}: agent '{config.id}' is not in the agents list of user.yaml "
                            "(it is ignored)")
    for agent_id in user.agents:
        if agent_id not in seen and not any(e.startswith(f"{user_id}/agents/") for e in errors):
            errors.append(f"{user_id}/user.yaml: lists agent '{agent_id}' but no file in agents/ defines it")
            continue
        try:
            config = load_agent_config(user_id, agent_id)
        except Exception:
            continue  # already reported above with its file name
        try:
            agent_default_model(user, config)
        except Exception as exc:
            errors.append(f"{user_id}/{agent_id}: {describe_config_error(exc)}")
        groups = config.chat_config.tool_groups or []
        for target in (config.delegation.targets if config.delegation is not None else []):
            if target.agent not in user.agents:
                warnings.append(f"{user_id}/{agent_id}: delegation target '{target.agent}' is not one of the user's agents "
                                "(task_start refuses it)")
        if "terminal" in groups:
            try:
                resolve_backends(user, config.terminal, agent_id=agent_id, allow_local=True)
            except InputError as exc:
                warnings.append(f"{user_id}/{agent_id}: the terminal tool group has no usable backend: {error_message('en', exc)}")
            else:
                if config.terminal and any(user.terminal_backends[n].type == "local" for n in config.terminal.backends
                                           if n in user.terminal_backends) and not TERMINAL.allow_local:
                    warnings.append(f"{user_id}/{agent_id}: uses a local backend; the server only accepts it with "
                                    "RECALL_TERMINAL_ALLOW_LOCAL=true or terminal.allow_local (not visible from here "
                                    "when the environment is scrubbed)")
    for var in sorted({user.provider.api_key, (user.embeddings_provider or user.provider).api_key} - {None, ""}):
        if not is_set(var):
            warnings.append(f"{user_id}: the key variable {var} is not set in this process's environment or secrets.env")
    return errors, warnings, len(user.agents)


def agent_check(user_id: str | None) -> int:
    users = [check_user_id(user_id)] if user_id else _user_ids()
    if user_id:
        _require_user(user_id)
    errors: list[str] = []
    warnings: list[str] = []
    agent_count = 0
    for uid in users:
        e, w, n = check_user(uid)
        errors += e
        warnings += w
        agent_count += n
    for line in warnings:
        print(f"warning: {line}")
    if errors:
        for line in errors:
            print(f"error: {line}")
        print(f"FAILED: {len(errors)} problem(s) in {len(users)} user(s). The running services keep their previous "
              "good configuration until this is fixed.")
        return 1
    print(f"OK: {len(users)} user(s), {agent_count} agent(s) load fine.")
    return 0


# ------------------------------------------------------------------ user commands


def user_show(user_id: str) -> int:
    _require_user(user_id)
    raw = yaml.safe_load((_user_dir(user_id) / "user.yaml").read_text(encoding="utf-8")) or {}
    try:
        user = UserConfig.model_validate(raw)
    except ValidationError as exc:
        raise AdminError("user.yaml is invalid: " + describe_config_error(exc)) from exc
    shown = user.model_dump(mode="json", exclude_none=True, exclude={"password_hash"})
    print(dump_yaml(shown).rstrip())
    print(f"# password_hash: {'set' if user.password_hash else 'not set'} (never shown); key variables are NAMES only")
    return 0


def user_set(user_id: str, assignments: list[str]) -> int:
    _require_user(user_id)
    if not assignments:
        raise AdminError("give at least one --set path=value")
    for item in assignments:
        parts, value = parse_assignment(item, UserConfig)
        if parts[0] in _PROTECTED_USER_PATHS:
            raise AdminError(f"'{parts[0]}' cannot be set here: {_PROTECTED_USER_PATHS[parts[0]]}.")
        if parts[-1] == "api_key" and (not isinstance(value, str) or not _ENV_NAME_RE.match(value) or redact(value) != value):
            raise AdminError(f"'{'.'.join(parts)}' takes the NAME of an environment variable (e.g. OPENROUTER_API_KEY), "
                             "never the key itself. Store the key with `python -m src.admin set-secret NAME`.")
    path = _user_dir(user_id) / "user.yaml"
    with exclusive_file_lock(_user_dir(user_id) / "agents.lock"):
        text = path.read_text(encoding="utf-8")
        raw = yaml.safe_load(text) or {}
        apply_validated(raw, assignments, UserConfig, lambda updated: _write_validated_user(path, text, updated))
    print(f"Updated user '{user_id}': {', '.join(_paths(assignments))}. Comments in user.yaml are not kept. "
          "Applies on the next message.")
    return 0


# ------------------------------------------------------------------ argparse wiring


def add_parsers(sub) -> None:
    agent = sub.add_parser("agent", help="list, show, create, change, rename, delete and check agents")
    asub = agent.add_subparsers(dest="agent_command", required=True)

    def with_user(parser, agent_arg: bool = False):
        parser.add_argument("--user", required=True, help="RECALL user id")
        if agent_arg:
            parser.add_argument("--agent", required=True, help="agent id")
        return parser

    with_user(asub.add_parser("list", help="the user's agents"))
    with_user(asub.add_parser("show", help="print an agent's yaml as the loader sees it"), True)
    create = with_user(asub.add_parser("create", help="create an agent (validated, same rules as the web UI)"))
    create.add_argument("--id", required=True, dest="agent_id", help="new agent id")
    create.add_argument("--name", help="display name (default: the id; 'Guide' for the guide template)")
    create.add_argument("--from-template", default="default", help="default (plain agent) or guide (setup guide)")
    create.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", dest="sets",
                        help="extra setting, dotted path = YAML value (repeatable)")
    setp = with_user(asub.add_parser("set", help="change settings of an existing agent"), True)
    setp.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", dest="sets",
                      help="dotted path = YAML value, e.g. chat_config.tool_groups=[memory_read,web] (repeatable)")
    rename = with_user(asub.add_parser("rename", help="change an agent id (moves its data folder) and/or name"), True)
    rename.add_argument("--new-id", required=True, help="the new agent id")
    rename.add_argument("--name", help="also set a new display name")
    delete = with_user(asub.add_parser("delete", help="delete an agent's configuration (data kept)"), True)
    delete.add_argument("--yes", action="store_true", help="confirm the deletion")
    check = asub.add_parser("check", help="load every user and agent with the real loader; exit 1 on errors")
    check.add_argument("--user", help="only this user")

    user = sub.add_parser("user", help="show or change non-secret user settings")
    usub = user.add_subparsers(dest="user_command", required=True)
    with_user(usub.add_parser("show", help="user.yaml (key variables by NAME, no password hash)"))
    uset = with_user(usub.add_parser("set", help="change non-secret user settings"))
    uset.add_argument("--set", action="append", default=[], metavar="PATH=VALUE", dest="sets",
                      help="dotted path = YAML value, e.g. provider.api_key=MY_KEY_NAME (repeatable)")


def run(args, service=None) -> int:
    """Run an `agent` or `user` command; prints `error: ...` to stderr and returns 1 on any failure."""
    from src.service.chat import ServiceError

    try:
        if args.command == "user":
            return user_show(args.user) if args.user_command == "show" else user_set(args.user, args.sets)
        command = args.agent_command
        if command == "check":
            return agent_check(args.user)
        service = service or _make_service()
        if command == "list":
            return agent_list(service, args.user)
        if command == "show":
            return agent_show(service, args.user, args.agent)
        if command == "create":
            return agent_create(service, args.user, args.agent_id, name=args.name, template=args.from_template,
                                assignments=args.sets)
        if command == "set":
            return agent_set(service, args.user, args.agent, args.sets)
        if command == "rename":
            return agent_rename(service, args.user, args.agent, args.new_id, args.name)
        return agent_delete(service, args.user, args.agent, yes=args.yes)
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
    except (ServiceError, InputError) as exc:
        print(f"error: {error_message('en', exc)}", file=sys.stderr)
    except (AgentConfigError, ValidationError, ValueError, FileNotFoundError, yaml.YAMLError, OSError) as exc:
        print(f"error: {describe_config_error(exc)}", file=sys.stderr)
    return 1
