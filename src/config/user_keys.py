"""A user's own OpenRouter key, set from the web UI.

The key lives in `secrets.env` under a variable of that user alone (`OPENROUTER_API_KEY_<USER>`), and only that
user's `user.yaml` is pointed at it, so nobody can overwrite another user's key. The value is never logged,
returned or put in an error message.
"""
from __future__ import annotations

import re

import httpx
import yaml

from src.config import secrets, settings
from src.utils.files import atomic_write_text, exclusive_file_lock
from src.utils.i18n import InputError

PREFIX = "OPENROUTER_API_KEY"
_KEY = re.compile(r"^[\x21-\x7e]{16,400}$")  # printable ASCII, no spaces
CHECK_URL = "https://openrouter.ai/api/v1/auth/key"


def variable_for(user_id: str) -> str:
    """`OPENROUTER_API_KEY_<USER>`: the user id in capitals, anything else as `_`."""
    return f"{PREFIX}_{re.sub(r'[^A-Za-z0-9]', '_', user_id).upper()}"


def check_key(key: str, *, timeout: float = 10.0) -> None:
    """Ask OpenRouter whether `key` is valid; raises a coded error when it is not or cannot be checked."""
    if not _KEY.match(key or ""):
        raise InputError("That does not look like an OpenRouter API key.", code="openrouter_key_bad_format")
    try:
        response = httpx.get(CHECK_URL, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except httpx.HTTPError:
        raise InputError("OpenRouter could not be reached to check the key. Try again.", code="openrouter_key_unchecked")
    if response.status_code in (401, 403):
        raise InputError("OpenRouter refused this key.", code="openrouter_key_refused")
    if response.status_code != 200:
        raise InputError("OpenRouter could not check the key right now. Try again.", code="openrouter_key_unchecked")


def _user_file(user_id: str):
    return settings.CONFIG_DIR / user_id / "user.yaml"


def _point_user_at(user_id: str, variable: str) -> None:
    """Make every OpenRouter key reference of this user's user.yaml name `variable` (YAML comments are lost)."""
    path = _user_file(user_id)
    with exclusive_file_lock(path.with_name(path.name + ".lock")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise InputError("Your user settings could not be updated.", code="openrouter_key_unsaved")
        changed = False
        for section in ("provider", "embeddings_provider"):
            block = data.get(section)
            if isinstance(block, dict) and block.get("name") == "openrouter" and block.get("api_key") != variable:
                block["api_key"] = variable
                changed = True
        if data.get("openrouter") != {"api_key": variable}:
            data["openrouter"] = {"api_key": variable}
            changed = True
        if changed:
            atomic_write_text(path, yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False))


def save_user_key(user_id: str, key: str) -> str:
    """Check and store `key` for `user_id`; returns the variable name used."""
    key = (key or "").strip()
    check_key(key)
    variable = variable_for(user_id)
    try:
        secrets.save_secret(variable, key)
        _point_user_at(user_id, variable)
    except (ValueError, OSError):
        raise InputError("The key could not be saved.", code="openrouter_key_unsaved")
    return variable


def remove_user_key(user_id: str) -> None:
    """Delete this user's own key from the secrets file (the user.yaml keeps naming the now empty variable)."""
    try:
        secrets.delete_secret(variable_for(user_id))
    except OSError:
        raise InputError("The key could not be removed.", code="openrouter_key_unsaved")


def key_state(user_id: str, current_variable: str) -> dict:
    """Whether the user has a key, and whether it is their own variable or one shared through the server."""
    own = variable_for(user_id)
    return {"configured": secrets.is_set(current_variable), "own": current_variable == own and secrets.is_set(own)}
