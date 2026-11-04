"""Administrator command: create RECALL users and set their web password.

    python -m src.admin create-user <user_id> [--name N] [--language en] [--api-key-env VAR]
                                    [--claude-login] [--generate]
    python -m src.admin set-password <user_id> [--generate]
    python -m src.admin route-test "message text" --user <user_id> [--force]

Passwords are never accepted as arguments (shell history): they are typed twice with
hidden input, or generated with --generate and printed once. Provider API keys are never
asked for or stored; only the NAME of the environment variable that holds them is.
"""

from __future__ import annotations

import argparse
import getpass
import re
import secrets
import sys
from pathlib import Path

import yaml

from src.config.loader import load_user
from src.config.models import ModelsConfig
from src.config.settings import CONFIG_DIR
from src.utils.files import atomic_write_text
from src.web.auth import hash_password

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # same rule as AgentStore / ChatService user ids
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")
_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789-_"  # no look-alike characters
_GENERATED_LENGTH = 24
_COMMON_PASSWORDS = frozenset({
    "12345678", "123456789", "1234567890", "password", "password1", "password123", "qwertyui",
    "qwerty123", "11111111", "00000000", "abcd1234", "iloveyou", "admin123", "letmein1", "recall123",
})
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"


class AdminError(Exception):
    """A user-facing failure; the message is printed without a traceback."""


def check_user_id(user_id: str) -> str:
    if not _ID_RE.match(user_id):
        raise AdminError("Invalid user id: use 1-64 letters, digits, '_' or '-'.")
    return user_id


def check_password_strength(password: str) -> None:
    """Reject short or obviously weak passwords (hash_password enforces the 8-character minimum too)."""
    if len(password) < 8:
        raise AdminError("The password must be at least 8 characters long.")
    lowered = password.lower()
    if lowered in _COMMON_PASSWORDS or len(set(password)) < 3:
        raise AdminError("That password is too common or too simple; choose another one.")


def generate_password() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_GENERATED_LENGTH))


def _read_password() -> str:
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Confirm:  "):
        raise AdminError("Passwords do not match.")
    return first


def _hash_new_password(generate: bool) -> tuple[str, str | None]:
    """(argon2 hash, the generated plaintext or None). Plaintext is only returned for --generate."""
    password = generate_password() if generate else _read_password()
    check_password_strength(password)
    try:
        hashed = hash_password(password)
    except ValueError as exc:
        raise AdminError(str(exc)) from exc
    return hashed, password if generate else None


def _write_yaml(path: Path, data: dict) -> None:
    atomic_write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False))


def _user_file(user_id: str) -> Path:
    return CONFIG_DIR / check_user_id(user_id) / "user.yaml"


def create_user(
    user_id: str, *, name: str | None = None, language: str = "en", api_key_env: str = DEFAULT_API_KEY_ENV,
    claude_login: bool = False, generate: bool = False,
) -> dict:
    """Create `<CONFIG_DIR>/<user_id>/user.yaml`; returns what the caller needs to print."""
    path = _user_file(user_id)
    if path.exists():
        raise AdminError(f"User '{user_id}' already exists; use `set-password` to change the password.")
    if not _LANGUAGE_RE.match(language):
        raise AdminError("Invalid language: use a code such as 'en' or 'es'.")
    if not _ENV_NAME_RE.match(api_key_env):
        raise AdminError("--api-key-env takes the NAME of an environment variable, never the key itself.")
    hashed, generated = _hash_new_password(generate)
    login_dir = (CONFIG_DIR / user_id / "claude-login").resolve()
    models = ModelsConfig()
    data: dict = {
        "id": user_id,
        "name": name or user_id,
        "password_hash": hashed,
        "language": language,
        "provider": {"name": "openrouter", "api_key": api_key_env},
        "models": {"chat": models.chat, "stt": models.stt, "embeddings": models.embeddings},
        "agents": [],
    }
    if claude_login:
        data["claude_subscription"] = {"claude_config_dir": login_dir.as_posix()}
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(path, data)
    try:
        load_user(user_id)
        if claude_login:
            login_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # never leave a user file that the app cannot load
        path.unlink(missing_ok=True)
        raise AdminError(f"The generated user.yaml is not valid, nothing was kept: {exc}") from exc
    return {"user_id": user_id, "generated": generated, "login_dir": login_dir.as_posix() if claude_login else None}


def set_password(user_id: str, *, generate: bool = False) -> str | None:
    """Replace only `password_hash`; returns the generated password for --generate."""
    path = _user_file(user_id)
    if not path.is_file():
        raise AdminError(f"User '{user_id}' does not exist.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AdminError(f"{path} is not a valid user.yaml.")
    data["password_hash"], generated = _hash_new_password(generate)
    _write_yaml(path, data)
    return generated


def _print_generated(user_id: str, password: str | None) -> None:
    if password:
        print(f"\nGenerated password for '{user_id}' (shown once, store it in a password manager):\n\n    {password}\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.admin", description="RECALL administrator commands.")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create-user", help="create a user with a hashed web password and no agents")
    create.add_argument("user_id")
    create.add_argument("--name", help="display name (default: the user id)")
    create.add_argument("--language", default="en", help="interface language code (default: en)")
    create.add_argument("--api-key-env", default=None, metavar="NAME",
                        help=f"name of the env var holding this user's provider key (default: {DEFAULT_API_KEY_ENV}; asked when omitted and interactive)")
    create.add_argument("--claude-login", action="store_true",
                        help="give the user their own Claude login folder under the config dir")
    create.add_argument("--generate", action="store_true", help="generate a strong password and print it once")
    setp = sub.add_parser("set-password", help="replace a user's web password (other keys are kept)")
    setp.add_argument("user_id")
    setp.add_argument("--generate", action="store_true", help="generate a strong password and print it once")
    route = sub.add_parser(
        "route-test", help="show how the Telegram orchestrator would rank a user's agents for a message",
    )
    route.add_argument("text", help="the message text")
    route.add_argument("--user", required=True, help="RECALL user id whose agents are ranked")
    route.add_argument("--force", action="store_true", help="run even when the orchestrator is disabled in settings")
    return parser


def route_test(user_id: str, text: str, *, force: bool = False) -> int:
    """Print the ranked agents with probabilities, confidence and latency (same code path as Telegram; no secret)."""
    import os
    import time

    from src.config.settings import ORCHESTRATOR
    from src.service.chat import ChatService

    if force:
        ORCHESTRATOR.enabled = True  # this process only
    service = ChatService()
    if not os.environ.get(ORCHESTRATOR.api_key, "").strip():
        print(f"error: the env var {ORCHESTRATOR.api_key} is not set", file=sys.stderr)
        return 1
    if not service.orchestrator_active(user_id):
        print("The orchestrator is off for this user, or the user has fewer than 2 agents (use --force to override "
              "the switch).", file=sys.stderr)
        return 1
    started = time.monotonic()
    decision = service.route_message(user_id, text)
    elapsed = (time.monotonic() - started) * 1000
    if decision is None:
        print("No decision (see the log; Telegram would use the preferred agent).", file=sys.stderr)
        return 1
    print(f"model: {ORCHESTRATOR.model}")
    for agent_id, probability in decision.ranking:
        mark = "*" if agent_id == decision.agent_id else " "
        print(f" {mark} {probability:6.3f}  {agent_id}  ({decision.agent_names.get(agent_id, agent_id)})")
    verdict = "uncertain" if decision.uncertain else "confident"
    print(f"choice: {decision.agent_id}  confidence: {decision.confidence:.3f} ({verdict}, "
          f"threshold {ORCHESTRATOR.min_confidence})")
    print(f"core agent: {decision.core_agent_id or '(none: preferred agent)'}")
    print(f"latency: {elapsed:.0f} ms total, {decision.latency_ms:.0f} ms API; cost: {decision.cost}")
    return 0


def _ask_api_key_env() -> str:
    if not sys.stdin.isatty():
        return DEFAULT_API_KEY_ENV
    answer = input(f"Env var NAME holding this user's provider key [{DEFAULT_API_KEY_ENV}]: ").strip()
    return answer or DEFAULT_API_KEY_ENV


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "route-test":
        return route_test(args.user, args.text, force=args.force)
    try:
        if args.command == "create-user":
            check_user_id(args.user_id)
            env_name = args.api_key_env or _ask_api_key_env()
            result = create_user(
                args.user_id, name=args.name, language=args.language, api_key_env=env_name,
                claude_login=args.claude_login, generate=args.generate,
            )
            _print_generated(args.user_id, result["generated"])
            print(f"Created {_user_file(args.user_id)}\n\nNext steps:")
            print(f"  1. Put the key in .env under the name {env_name} (this command never stores keys).")
            print(f"  2. Telegram (optional): add `<telegram_id>={args.user_id}` to TELEGRAM_ALLOWED_USERS in .env "
                  "(comma-separated); .env is never edited for you.")
            if result["login_dir"]:
                print("  3. Claude login, done by the person with their OWN account:\n"
                      f"     docker compose exec -it recall-web sh -c 'CLAUDE_CONFIG_DIR={result['login_dir']} claude auth login'")
            print("  Then restart the services if they were running, and ask the user to create their first agent "
                  "in the web UI (Agents > New agent).")
        else:
            _print_generated(args.user_id, set_password(args.user_id, generate=args.generate))
            print(f"Password updated for '{args.user_id}'. Other keys were kept; YAML comments in user.yaml are lost.")
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
