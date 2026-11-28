"""Fetch the bytes of a file the agent can already read: from one of its sources or from a terminal backend.

`<source>:<path>` goes through the source resolution (hidden entries, protected paths and unreadable sources are refused);
`<backend>:<path>` goes through the terminal session of that backend, so the approval rules, the protected paths and the
audit trail are exactly those of a read there. A source wins when a backend has the same name.
"""
from __future__ import annotations

import posixpath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.agent import Agent


class FileAccessError(Exception):
    """The file cannot be fetched; the message is for the agent."""


def backend_names(agent: Agent) -> list[str]:
    return list(getattr(agent, "terminal_sessions", None) or {})


def fetch_file(agent: Agent, address: str, max_bytes: int) -> tuple[str, bytes]:
    """(file name, bytes) of `<source>:<path>` or `<backend>:<path>`; FileAccessError when it cannot be had."""
    name, sep, rel = address.partition(":")
    if not sep:
        raise FileAccessError(f"invalid address (expected 'source:path' or 'backend:path'): {address!r}")
    sessions = getattr(agent, "terminal_sessions", None) or {}
    try:
        source = agent.sources.get(name)
    except KeyError:
        source = None
    if source is not None:
        return _from_source(source, rel, address, max_bytes)
    if name in sessions:
        return _from_backend(sessions[name], rel, max_bytes)
    known = [*agent.sources.names(), *sessions]
    raise FileAccessError(f"unknown source or backend {name!r}; use one of: {', '.join(known)}")


def _from_source(source, rel: str, address: str, max_bytes: int) -> tuple[str, bytes]:
    if not source.readable:
        raise FileAccessError(f"'{source.name}' is not readable here")
    try:
        target = source.resolve(rel)
    except ValueError as exc:
        raise FileAccessError(str(exc)) from exc
    if not target.is_file():
        raise FileAccessError(f"'{address}' is not a file")
    size = target.stat().st_size
    if size > max_bytes:
        raise FileAccessError(f"the file is {size // 1024} KiB; the limit is {max_bytes // 1024 // 1024} MiB")
    try:
        return target.name, source.read_bytes(rel)
    except OSError as exc:
        raise FileAccessError(f"could not read the file ({type(exc).__name__})") from exc


def _from_backend(session, path: str, max_bytes: int) -> tuple[str, bytes]:
    from src.terminal import files

    try:
        data = files.fetch_bytes(session, path, max_bytes)
    except ValueError as exc:
        raise FileAccessError(str(exc)) from exc
    return posixpath.basename(path.rstrip("/")) or "file", data


def check_file(agent: Agent, address: str, max_bytes: int) -> tuple[str, str, str]:
    """(file name, root, path) of a file the agent can read, without keeping its bytes; FileAccessError when it cannot.

    The same resolution, refusals and size limit as `fetch_file`. A source file is only stat-ed; a backend file goes through
    the backend's session exactly like a fetch (approval, protected paths, audit) and the bytes are dropped. `root` is the
    source name or `backend:<name>`; `path` is the part after the first colon.
    """
    name, _, rel = address.partition(":")
    sessions = getattr(agent, "terminal_sessions", None) or {}
    try:
        source = agent.sources.get(name) if name else None
    except KeyError:
        source = None
    if source is None:
        file_name, _data = fetch_file(agent, address, max_bytes)  # backend, or the usual error for a bad address
        return file_name, f"backend:{name}", rel
    if not source.readable:
        raise FileAccessError(f"'{source.name}' is not readable here")
    try:
        target = source.resolve(rel)
    except ValueError as exc:
        raise FileAccessError(str(exc)) from exc
    if not target.is_file():
        raise FileAccessError(f"'{address}' is not a file")
    size = target.stat().st_size
    if size > max_bytes:
        raise FileAccessError(f"the file is {size // 1024} KiB; the limit is {max_bytes // 1024 // 1024} MiB")
    return target.name, name, rel
