"""Fast YAML parsing for configuration files that are read on every request.

`yaml.safe_load` uses the pure-Python parser; the C one (libyaml) gives the same result about ten times faster when
it is installed. Config files are also re-read many times per request (every agent file to find one id, the user file
for each agent), so a parsed file is kept until it changes on disk: its modification time, size and inode (a file
replaced atomically gets a new inode even within one clock tick). Callers get a deep copy, so changing the result
never changes the cache.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path

import yaml

_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_CACHE: dict[str, tuple[tuple[int, int, int], object]] = {}
_CACHE_MAX = 512
_LOCK = threading.Lock()


def parse_yaml(text: str) -> object:
    """`yaml.safe_load` with the C parser when available."""
    return yaml.load(text, Loader=_LOADER)


def load_yaml_file(path: Path) -> object:
    """Parse a YAML file, reusing the last result while the file is unchanged.

    Raises what reading and parsing raise (OSError, UnicodeError, yaml.YAMLError); a failure is not cached.
    """
    info = path.stat()
    key = (info.st_mtime_ns, info.st_size, info.st_ino)
    where = str(path)
    with _LOCK:
        cached = _CACHE.get(where)
    if cached is not None and cached[0] == key:
        return copy.deepcopy(cached[1])
    data = parse_yaml(path.read_text(encoding="utf-8"))
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            _CACHE.clear()  # bounded: the next reads refill it
        _CACHE[where] = (key, data)
    return copy.deepcopy(data)


def clear_yaml_cache() -> None:
    with _LOCK:
        _CACHE.clear()
