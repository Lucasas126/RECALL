"""Secrets file fallback: `<config_dir>/secrets.env` (KEY=VALUE lines), consulted when the variable is not set.

The real process environment always wins. The file is re-read when its modification time or size changes, so a key
saved with `python -m src.admin set-secret NAME` is picked up by the running web and Telegram processes on the next
message, without a restart. Values are never logged or printed by this module.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from src.config import settings
from src.utils.files import atomic_write_text, exclusive_file_lock

SECRETS_FILE_NAME = "secrets.env"
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")

_lock = threading.Lock()
_cache: tuple[Path, tuple[int, int, int], dict[str, str]] | None = None


def secrets_file() -> Path:
    """The fallback file inside the configured config dir (read on every call so tests can point it elsewhere)."""
    return Path(settings.CONFIG_DIR) / SECRETS_FILE_NAME


def check_name(name: str) -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("secret names use 3-64 characters: capital letters, digits and '_', starting with a letter")
    return name


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse(text: str) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and `#` comments are ignored; one pair of surrounding quotes is removed."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if match:
            found[match.group(1)] = _unquote(match.group(2))
    return found


def read_secrets() -> dict[str, str]:
    """All pairs of the secrets file ({} when it does not exist), cached until the file changes."""
    global _cache
    path = secrets_file()
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)  # an atomic replace gets a new inode
    with _lock:
        if _cache is not None and _cache[0] == path and _cache[1] == stamp:
            return _cache[2]
        try:
            values = parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            return {}
        _cache = (path, stamp, values)
        return values


def get_secret(name: str, default: str = "") -> str:
    """The value of `name`: the process environment first (when non-empty), then the secrets file."""
    value = os.environ.get(name)
    if value is not None and value.strip():
        return value
    return read_secrets().get(name, default)


def is_set(name: str) -> bool:
    return bool(get_secret(name).strip())


def file_values() -> list[str]:
    """Values stored in the file, for output redaction only."""
    return list(read_secrets().values())


def fingerprint() -> str:
    """Changes whenever the file changes (so runtimes that hold a resolved key are rebuilt); never the content."""
    try:
        stat = secrets_file().stat()
    except OSError:
        return ""
    return f"{stat.st_mtime_ns}:{stat.st_size}:{stat.st_ino}"


def save_secret(name: str, value: str) -> None:
    """Set one pair atomically (mode 0600), keeping the other lines and comments of the file."""
    check_name(name)
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("a secret must be a single non-empty line")
    path = secrets_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path.with_name(path.name + ".lock")):
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        new_line = f"{name}={value}"
        for index, line in enumerate(lines):
            match = _LINE_RE.match(line)
            if match and match.group(1) == name and not line.lstrip().startswith("#"):
                lines[index] = new_line
                break
        else:
            lines.append(new_line)
        atomic_write_text(path, "\n".join(lines) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def delete_secret(name: str) -> None:
    """Remove one pair from the file, keeping the other lines and comments (nothing happens when it is absent)."""
    check_name(name)
    path = secrets_file()
    if not path.exists():
        return
    with exclusive_file_lock(path.with_name(path.name + ".lock")):
        lines = path.read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if not ((m := _LINE_RE.match(line)) and m.group(1) == name and not line.lstrip().startswith("#"))]
        if len(kept) != len(lines):
            atomic_write_text(path, "\n".join(kept) + ("\n" if kept else ""))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


def copy_secret(source: str, target: str) -> bool:
    """Copy the file value of `source` to `target` (overwriting it); True when `target` already existed. Never returns a value."""
    check_name(source)
    check_name(target)
    if source == target:
        raise ValueError("source and target are the same variable")
    value = read_secrets().get(source, "")
    if not value.strip():
        raise KeyError(source)
    existed = target in read_secrets()
    save_secret(target, value)
    return existed


def rename_secret(old: str, new: str) -> None:
    """Move a value to a new name in the file; refuses to overwrite an existing `new`."""
    check_name(old)
    check_name(new)
    if old == new:
        raise ValueError("the names are the same")
    values = read_secrets()
    if old not in values:
        raise KeyError(old)
    if new in values:
        raise FileExistsError(new)
    save_secret(new, values[old])
    delete_secret(old)
