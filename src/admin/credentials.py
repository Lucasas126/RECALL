"""Credential commands: store provider keys in `.config/secrets.env` without ever showing them.

    python -m src.admin set-secret NAME [--stdin]   # value from hidden input, or piped with --stdin
    python -m src.admin secret list                 # names and whether each is set, never values

The value is never an argument (shell history, process list, approval cards) and is never printed.
"""

from __future__ import annotations

import getpass
import os
import sys

from src.admin.users import AdminError
from src.config import secrets
from src.config.loader import load_user
from src.config.settings import APPROVAL_EXPLAIN, ASSOC, CHAT_NAMING, CONFIG_DIR, ORCHESTRATOR, TAVILY, TELEGRAM_ALLOWED_USERS_ENV


def _user_ids_with_keys() -> list[tuple[str, str]]:
    """(where, variable NAME) for every key variable the users' yaml refers to; unreadable users are skipped."""
    found: list[tuple[str, str]] = []
    if not CONFIG_DIR.is_dir():
        return found
    for folder in sorted(CONFIG_DIR.iterdir()):
        if not (folder / "user.yaml").is_file():
            continue
        try:
            user = load_user(folder.name)
        except Exception:
            continue
        pairs = [("provider.api_key", user.provider.api_key), ("openrouter.api_key", user.openrouter.api_key)]
        if user.embeddings_provider is not None:
            pairs.append(("embeddings_provider.api_key", user.embeddings_provider.api_key))
        found.extend((f"user {folder.name} {label}", name) for label, name in pairs if name)
    return found


def set_secret(name: str, *, use_stdin: bool) -> int:
    try:
        secrets.check_name(name)
    except ValueError as exc:
        raise AdminError(f"invalid name '{name}': {exc}") from exc
    if use_stdin:
        value = sys.stdin.read().rstrip("\r\n")
    else:
        value = getpass.getpass(f"Value for {name} (hidden): ")
        if value != getpass.getpass("Repeat it: "):
            raise AdminError("the two entries differ; nothing was saved")
    if not value.strip():
        raise AdminError("the value is empty; nothing was saved")
    try:
        secrets.save_secret(name, value)
    except (ValueError, OSError) as exc:
        raise AdminError(f"could not save {name}: {exc}") from exc
    print(f"saved {name}")
    return 0


def known_names() -> dict[str, list[str]]:
    """Variable NAMES the configuration refers to, with where they are used."""
    used: dict[str, list[str]] = {}

    def add(name: str | None, where: str) -> None:
        if name:
            used.setdefault(name, []).append(where)

    for where, name in _user_ids_with_keys():
        add(name, where)
    add(TAVILY.api_key, "settings tavily.api_key")
    add(ASSOC.api_key, "settings associative_memory.api_key")
    add(CHAT_NAMING.api_key, "settings chat_naming.api_key")
    add(APPROVAL_EXPLAIN.api_key, "settings approval_explain.api_key")
    add(ORCHESTRATOR.api_key, "settings orchestrator.api_key")
    add("TELEGRAM_BOT_TOKEN", "telegram bot")
    add(TELEGRAM_ALLOWED_USERS_ENV, "telegram allowed users")
    return used


def secret_list() -> int:
    used = known_names()
    in_file = secrets.read_secrets()
    names = sorted(set(used) | set(in_file))
    width = max((len(n) for n in names), default=4)
    any_missing = False
    for name in names:
        env = bool(os.environ.get(name, "").strip())
        file = bool(in_file.get(name, "").strip())
        if env:
            status = "set (environment" + ("; overrides the file entry)" if file else ")")
        elif file:
            status = "set (secrets.env)"
        else:
            status, any_missing = "NOT SET", True
        print(f"{name:<{width}}  {status}  {'; '.join(used.get(name, ['(only in secrets.env)']))}")
    if not names:
        print("No secret names are referenced yet.")
    if any_missing:
        print("Note: this checks the environment of THIS process and secrets.env. The server's own environment (.env, "
              "docker env) is hidden from terminal commands, so a variable may be set there although it shows NOT SET.")
    return 0
