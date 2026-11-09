"""Administrator command: write the permission levels that older configurations leave implicit.

    python -m src.admin migrate-levels [--dry-run]

For every user without `max_level` it writes the effective value (admin on a single-user installation, else
collaborator). For every agent without a declared `level` it writes the lowest level covering its configuration,
unless that is above the owner's `max_level`: the agent is then left untouched and a conflict line says so. Conflicts
are reported, not fatal (exit code 0); an unreadable file is an error (exit code 1). Running it again changes nothing.
Existing files are edited by appending one top-level line, so comments and layout survive.
"""

from __future__ import annotations

import sys

import yaml
from pydantic import ValidationError

from src.admin.users import AdminError
from src.config.agents import AgentStore
from src.config.errors import describe_config_error
from src.config.levels import effective_user_level, installation_user_count, required_level
from src.config.models import AgentConfig, UserConfig
from src.config.settings import CONFIG_DIR, DATA_DIR
from src.utils.files import atomic_write_text, exclusive_file_lock


def _append_key(path, text: str, key: str, value: str, model) -> None:
    """Append `key: value` as a top-level line, validate the result with `model`, then write it."""
    updated = text if text.endswith("\n") or not text else text + "\n"
    updated += f"{key}: {value}\n"
    model.model_validate(yaml.safe_load(updated))  # ValidationError: nothing is written
    atomic_write_text(path, updated)


def migrate_levels(*, dry_run: bool = False) -> tuple[list[tuple[str, ...]], list[str], list[str]]:
    """(rows, conflicts, errors): rows are (kind, user, agent, action, level)."""
    from src.admin.agents import _user_ids  # lazy: agents imports the users module this one also needs

    rows: list[tuple[str, ...]] = []
    conflicts: list[str] = []
    errors: list[str] = []
    store = AgentStore(CONFIG_DIR, DATA_DIR)
    count = installation_user_count(CONFIG_DIR)
    for user_id in _user_ids():
        user_dir = CONFIG_DIR / user_id
        with exclusive_file_lock(user_dir / "agents.lock"):
            user_path = user_dir / "user.yaml"
            try:
                text = user_path.read_text(encoding="utf-8")
                user = UserConfig.model_validate(yaml.safe_load(text))
            except (OSError, ValidationError, yaml.YAMLError) as exc:
                errors.append(f"{user_id}/user.yaml: {describe_config_error(exc)}")
                continue
            if user.max_level is None:
                max_level = effective_user_level(user, count, warn=False)
                if not dry_run:
                    try:
                        _append_key(user_path, text, "max_level", max_level.label, UserConfig)
                    except (OSError, ValidationError, yaml.YAMLError) as exc:
                        errors.append(f"{user_id}/user.yaml: {describe_config_error(exc)}")
                        continue
                rows.append(("user", user_id, "-", "would set" if dry_run else "set", max_level.label))
            else:
                max_level = user.max_level
                rows.append(("user", user_id, "-", "kept", max_level.label))
            for path in store._agent_files(user_id):
                try:
                    agent_text = path.read_text(encoding="utf-8")
                    agent = AgentConfig.model_validate(yaml.safe_load(agent_text))
                except (OSError, ValidationError, yaml.YAMLError) as exc:
                    errors.append(f"{user_id}/agents/{path.name}: {describe_config_error(exc)}")
                    continue
                if agent.level is not None:
                    rows.append(("agent", user_id, agent.id, "kept", agent.level.label))
                    continue
                needed = required_level(agent, user.terminal_backends)
                if needed > max_level:
                    conflicts.append(
                        f"conflict: user={user_id} agent={agent.id} required={needed.label} max_level={max_level.label}"
                    )
                    rows.append(("agent", user_id, agent.id, "conflict", needed.label))
                    continue
                if not dry_run:
                    try:
                        _append_key(path, agent_text, "level", needed.label, AgentConfig)
                    except (OSError, ValidationError, yaml.YAMLError) as exc:
                        errors.append(f"{user_id}/agents/{path.name}: {describe_config_error(exc)}")
                        continue
                rows.append(("agent", user_id, agent.id, "would set" if dry_run else "set", needed.label))
    return rows, conflicts, errors


def run(args) -> int:
    try:
        rows, conflicts, errors = migrate_levels(dry_run=args.dry_run)
    except (AdminError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    header = ("kind", "user", "agent", "action", "level")
    table = [header, *rows]
    widths = [max(len(row[i]) for row in table) for i in range(len(header))]
    for row in table:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    for line in conflicts:
        print(line)
    for line in errors:
        print(f"error: {line}", file=sys.stderr)
    changed = sum(1 for row in rows if row[3] in ("set", "would set"))
    verb = "would change" if args.dry_run else "changed"
    print(f"{verb} {changed} file(s), {len(conflicts)} conflict(s), {len(errors)} error(s)"
          + (" (dry run: nothing written)" if args.dry_run else "") + ".")
    if conflicts:
        print("Conflicts are left untouched: raise the user's max_level (`user set --set max_level=...`), "
              "lower the agent's tools or set its level by hand.")
    return 1 if errors else 0


def add_parsers(sub) -> None:
    migrate = sub.add_parser(
        "migrate-levels", help="write max_level to users and level to agents that lack them (idempotent)",
    )
    migrate.add_argument("--dry-run", action="store_true", help="show what would change without writing")
