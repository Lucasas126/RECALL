"""Read-only file viewer/explorer over an agent's sources and terminal backends (logic behind `/api/files/*`).

Sources go through `Source.resolve/list/read_bytes`; backends through the agent's terminal sessions' backend with the
protected paths enforced and every read audited. Backends listed in `file_panel.open_backends` need no approval inside
the panel; any other needs a grant the user gives once per web session.
"""
from __future__ import annotations

import base64
import binascii
import os
import posixpath
import shlex
import threading
import time
from dataclasses import dataclass
from typing import Any

from src.config.settings import FILE_PANEL
from src.terminal.audit import audit_writable, write_audit
from src.utils.file_kinds import file_kind

GRANT_TTL = 8 * 3600
BACKEND_PREFIX = "backend:"
_TIMEOUT = 30


class FileError(Exception):
    """A request the panel refuses; `status` is the HTTP status and `body` the JSON answer."""

    def __init__(self, status: int, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.body: dict[str, Any] = {"error": message, **extra}


class FileGrants:
    """In-memory grants of non-open backends: per web session, user, agent and backend, expiring after 8 hours."""

    def __init__(self, ttl: float = GRANT_TTL, clock=time.time):
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[tuple[str, str, str, str], float] = {}

    def grant(self, sid: str, user: str, agent: str, backend: str) -> None:
        with self._lock:
            self._grants[(sid, user, agent, backend)] = self._clock() + self._ttl

    def granted(self, sid: str, user: str, agent: str, backend: str) -> bool:
        key = (sid, user, agent, backend)
        with self._lock:
            expiry = self._grants.get(key)
            if expiry is not None and expiry <= self._clock():
                del self._grants[key]
                return False
            return expiry is not None

    def drop_session(self, sid: str) -> None:
        with self._lock:
            for key in [k for k in self._grants if k[0] == sid]:
                del self._grants[key]


@dataclass
class Entry:
    name: str
    type: str
    size: int
    mtime: int

    def as_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "size": self.size, "mtime": self.mtime}


def clean_path(path: str) -> str:
    """The panel path, relative and normalized; '' is the root. Absolute paths, `..` and control characters are refused."""
    if not isinstance(path, str) or "\x00" in path or "\n" in path or "\r" in path or len(path) > 4096:
        raise FileError(400, "invalid path")
    path = path.strip()
    if path.startswith(("/", "~", "\\")):
        raise FileError(400, "absolute paths are not allowed")
    parts = [p for p in path.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise FileError(400, "path must not contain '..'")
    return "/".join(parts)


class FilePanel:
    """The roots of one agent and the read-only operations on them."""

    def __init__(self, user_id: str, agent_id: str, sources, sessions: dict | None, grants: FileGrants, sid: str):
        self.user_id = user_id
        self.agent_id = agent_id
        self.sources = sources
        self.sessions = sessions or {}
        self.grants = grants
        self.sid = sid
        self.max_bytes = FILE_PANEL.max_file_mib * 1024 * 1024
        self.max_entries = FILE_PANEL.max_entries

    # ------------------------------------------------------------------ roots
    def _is_open(self, backend: str) -> bool:
        return backend in FILE_PANEL.open_backends

    def _granted(self, backend: str) -> bool:
        return self._is_open(backend) or self.grants.granted(self.sid, self.user_id, self.agent_id, backend)

    def roots(self) -> list[dict]:
        roots = [{"id": n, "label": n, "kind": "source", "open": True, "granted": True} for n in self.sources.names()]
        for name in self.sessions:
            roots.append({
                "id": BACKEND_PREFIX + name, "label": name, "kind": "backend",
                "open": self._is_open(name), "granted": self._granted(name),
            })
        return roots

    def grant(self, backend: str) -> None:
        if backend not in self.sessions:
            raise FileError(404, "unknown backend")
        self.grants.grant(self.sid, self.user_id, self.agent_id, backend)

    def _target(self, root: str):
        """('source', Source) or ('backend', session) for a root id; FileError when unknown or not allowed."""
        if root.startswith(BACKEND_PREFIX):
            name = root[len(BACKEND_PREFIX):]
            session = self.sessions.get(name)
            if session is None:
                raise FileError(404, "unknown root")
            if not self._granted(name):
                raise FileError(403, "approval_required", backend=name)
            return "backend", session
        if root not in self.sources.names():
            raise FileError(404, "unknown root")
        return "source", self.sources.get(root)

    # ------------------------------------------------------------------ operations
    def list(self, root: str, path: str) -> dict:
        path = clean_path(path)
        kind, target = self._target(root)
        entries, truncated = self._list_source(target, path) if kind == "source" else self._list_backend(target, path)
        entries.sort(key=lambda e: (e.type != "dir", e.name.lower(), e.name))
        return {"root": root, "path": path, "entries": [e.as_dict() for e in entries], "truncated": truncated}

    def info(self, root: str, path: str) -> dict:
        path = clean_path(path)
        if not path:
            raise FileError(400, "path required")
        kind, target = self._target(root)
        name = posixpath.basename(path)
        size = self._source_file(target, path).stat().st_size if kind == "source" else self._backend_stat(target, path)
        file_type, mime = file_kind(name)
        pages = None
        if file_type == "pdf" and size <= self.max_bytes:
            pages = self._pdf_pages(self._bytes(kind, target, path))
        return {"name": name, "size": size, "mime": mime, "kind": file_type, "pages": pages}

    def content(self, root: str, path: str) -> tuple[str, bytes]:
        path = clean_path(path)
        if not path:
            raise FileError(400, "path required")
        kind, target = self._target(root)
        return posixpath.basename(path), self._bytes(kind, target, path)

    def pdf_page(self, root: str, path: str, page: int, width: int) -> bytes:
        name, data = self.content(root, path)
        if file_kind(name)[0] != "pdf":
            raise FileError(400, "not a PDF")
        import pymupdf

        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                if not 1 <= page <= len(doc):
                    raise FileError(404, "no such page")
                pg = doc[page - 1]
                zoom = max(200, min(int(width), 2000)) / max(pg.rect.width, 1)
                return pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).tobytes("png")
        except FileError:
            raise
        except Exception as exc:
            raise FileError(400, "not a readable PDF") from exc

    @staticmethod
    def _pdf_pages(data: bytes) -> int | None:
        import pymupdf

        try:
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                return len(doc)
        except Exception:
            return None

    def _bytes(self, kind: str, target, path: str) -> bytes:
        if kind == "backend":
            return self._backend_read(target, path)
        file = self._source_file(target, path)
        if file.stat().st_size > self.max_bytes:
            raise FileError(413, "file too large")
        try:
            return target.read_bytes(path)
        except (OSError, ValueError, PermissionError) as exc:
            raise FileError(404, "cannot read file") from exc

    # ------------------------------------------------------------------ sources
    @staticmethod
    def _hidden(path: str) -> bool:
        return any(part.startswith(".") for part in path.split("/") if part)

    def _source_file(self, source, path: str):
        if self._hidden(path):
            raise FileError(403, "hidden entries are not available")
        try:
            file = source.resolve(path)
        except (ValueError, PermissionError, OSError) as exc:
            raise FileError(403, "path not allowed") from exc
        if not file.is_file():
            raise FileError(404, "not found")
        return file

    def _list_source(self, source, path: str) -> tuple[list[Entry], bool]:
        if self._hidden(path):
            raise FileError(403, "hidden entries are not available")
        try:
            folder = source.resolve(path)
        except (ValueError, PermissionError, OSError) as exc:
            raise FileError(403, "path not allowed") from exc
        if not folder.is_dir():
            raise FileError(404, "not found")
        entries: list[Entry] = []
        truncated = False
        for item in source.list(path):
            rel = item.rstrip("/")
            name = posixpath.basename(rel)
            if name.startswith("."):
                continue
            try:
                info = source.resolve(rel).stat()
            except (ValueError, PermissionError, OSError):
                continue  # a link leaving the source, or a vanished entry
            if len(entries) >= self.max_entries:
                truncated = True
                break
            is_dir = item.endswith("/")
            entries.append(Entry(name, "dir" if is_dir else "file", 0 if is_dir else info.st_size, int(info.st_mtime)))
        return entries, truncated

    # ------------------------------------------------------------------ backends
    def _protected(self, session, path: str) -> None:
        protected = session.protected()
        if protected is None:
            return
        cwd = session.local_cwd()
        candidates = [path]
        if session.local:
            full = path if posixpath.isabs(path) else posixpath.join(session.workdir, path)
            candidates.append(os.path.realpath(os.path.expanduser(full)))
        if any(protected.reason(candidate, cwd) is not None for candidate in candidates):
            raise FileError(403, "protected path")

    def _run(self, session, tool: str, display: str, script: str):
        if not audit_writable(session.audit_file):
            raise FileError(403, "audit trail not writable")
        started = time.monotonic()
        result = session.backend.run(script, session.workdir, _TIMEOUT, None)
        write_audit(
            session.audit_file, user=session.user_id, agent=session.agent_id, backend=session.name, tool=tool,
            command=display, cwd=session.workdir, decision="approved:file-panel", exit_code=result.exit_code,
            duration_ms=int((time.monotonic() - started) * 1000), timed_out=result.timed_out,
        )
        if result.timed_out:
            raise FileError(504, "timed out")
        return result

    @staticmethod
    def _word(path: str) -> str:
        if path == "":
            return "."
        return shlex.quote("./" + path if path.startswith("-") else path)

    def _backend_stat(self, session, path: str) -> int:
        self._protected(session, path)
        script = f'f={self._word(path)}\n[ -e "$f" ] || exit 3\n[ -d "$f" ] && exit 4\nstat -L -c %s -- "$f" || exit 2'
        result = self._run(session, "file_panel_stat", f"stat {path}", script)
        if result.exit_code == 3:
            raise FileError(404, "not found")
        if result.exit_code == 4:
            raise FileError(400, "is a directory")
        if result.exit_code != 0 or not result.stdout.strip().isdigit():
            raise FileError(404, "cannot stat")
        return int(result.stdout.strip())

    def _backend_read(self, session, path: str) -> bytes:
        self._protected(session, path)
        script = "\n".join([
            f"f={self._word(path)}",
            '[ -e "$f" ] || exit 3', '[ -d "$f" ] && exit 4',
            'n=$(wc -c < "$f" | tr -d " ")',
            f'if [ "$n" -gt {int(self.max_bytes)} ]; then exit 6; fi',
            "base64 < \"$f\" | tr -d '\\n\\r'",
        ])
        result = self._run(session, "file_panel_read", f"read {path}", script)
        if result.exit_code == 6:
            raise FileError(413, "file too large")
        if result.exit_code == 3:
            raise FileError(404, "not found")
        if result.exit_code == 4:
            raise FileError(400, "is a directory")
        if result.exit_code != 0:
            raise FileError(404, "cannot read file")
        try:
            return base64.b64decode(result.stdout.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise FileError(404, "cannot read file") from exc

    def _list_backend(self, session, path: str) -> tuple[list[Entry], bool]:
        self._protected(session, path)
        script = f'f={self._word(path)}\n[ -d "$f" ] || exit 3\nls -lAL --time-style=+%s -- "$f" || exit 2'
        result = self._run(session, "file_panel_list", f"ls {path or '.'}", script)
        if result.exit_code == 3:
            raise FileError(404, "not found")
        if result.exit_code != 0:
            raise FileError(404, "cannot list")
        protected = session.protected()
        entries: list[Entry] = []
        truncated = False
        for line in result.stdout.splitlines():
            parts = line.split(None, 6)
            if len(parts) < 7 or parts[0][:1] not in ("-", "d") or not parts[4].isdigit() or not parts[5].isdigit():
                continue
            name = parts[6]
            if "/" in name:
                continue
            if protected is not None and protected.reason(posixpath.join(path, name), session.local_cwd()):
                continue
            if len(entries) >= self.max_entries:
                truncated = True
                break
            is_dir = parts[0][0] == "d"
            entries.append(Entry(name, "dir" if is_dir else "file", 0 if is_dir else int(parts[4]), int(parts[5])))
        return entries, truncated
