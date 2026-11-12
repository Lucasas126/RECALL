"""Background processes of the terminal: start a long command, come back to it by id.

A process record lives as one JSON file under `<data_dir>/<user>/.processes/` next to its log. Local processes
run in their own process group with stdout and stderr merged into a rolling log (a reader thread keeps the last
`log_cap` bytes); a watcher records the exit code and enforces the maximum lifetime. A restart cannot reattach
to a local process (it dies with the container), so `recover` marks the records it finds running as `lost`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.terminal.backends import kill_tree
from src.utils.logger import get_logger

logger = get_logger("processes")

PROCESS_ID_RE = re.compile(r"^p[0-9a-f]{8}$")
RUNNING = "running"
FINISHED_STATES = ("exited", "stopped", "timed_out", "lost", "failed")
DEFAULT_LOG_CAP = 200_000  # bytes kept of a process output
DEFAULT_MAX_LIFETIME = 2 * 3600  # seconds a process may run before it is stopped
DEFAULT_MAX_PER_CHAT = 5
DEFAULT_MAX_PER_USER = 10
MAX_TAIL_LINES = 500
_CHUNK = 8192


class ProcessError(Exception):
    """A refusal the model can read: unknown id, limit reached, nothing to stop."""


@dataclass
class _Live:
    """What only this server process holds about a running local process."""

    proc: subprocess.Popen | None = None
    done: threading.Event = field(default_factory=threading.Event)
    reader: threading.Thread | None = None
    stop_reason: str = ""  # 'stopped' or 'timed_out' once someone asked the process to end


class ProcessRegistry:
    """Durable process records plus the live local processes of this server process."""

    def __init__(
        self, data_dir: Path, *, log_cap: int = DEFAULT_LOG_CAP, max_lifetime: float = DEFAULT_MAX_LIFETIME,
        max_per_chat: int = DEFAULT_MAX_PER_CHAT, max_per_user: int = DEFAULT_MAX_PER_USER,
    ) -> None:
        self._data_dir = Path(data_dir)
        self.log_cap = log_cap
        self.max_lifetime = max_lifetime
        self.max_per_chat = max_per_chat
        self.max_per_user = max_per_user
        self._live: dict[tuple[str, str], _Live] = {}
        self._lock = threading.RLock()
        self._on_finish: list = []  # callbacks(record) when a process ends (the wake hook of step d)

    # ---- storage
    def folder(self, user_id: str, *, create: bool = True) -> Path:
        path = self._data_dir / user_id / ".processes"
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _record_path(self, user_id: str, process_id: str) -> Path:
        if not PROCESS_ID_RE.match(process_id):
            raise ProcessError(f"unknown process: {process_id!r}")
        return self.folder(user_id) / f"{process_id}.json"

    def log_path(self, user_id: str, process_id: str) -> Path:
        return self._record_path(user_id, process_id).with_suffix(".log")

    def _save(self, record: dict) -> None:
        path = self._record_path(record["user"], record["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def get(self, user_id: str, process_id: str) -> dict:
        path = self._record_path(user_id, process_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ProcessError(f"unknown process: {process_id!r}") from None

    def list(self, user_id: str, chat_id: str | None = None) -> list[dict]:
        records = []
        for path in sorted(self.folder(user_id, create=False).glob("p*.json")) if self.folder(user_id, create=False).is_dir() else []:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if chat_id is None or record.get("chat_id") == chat_id:
                records.append(record)
        return sorted(records, key=lambda r: r.get("started", 0))

    def subscribe_finish(self, callback) -> None:
        self._on_finish.append(callback)

    # ---- start
    def start_local(
        self, *, user_id: str, agent_id: str, chat_id: str, backend: str, command: str, argv: list[str], cwd: str,
        env: dict[str, str], notify: bool = True, max_lifetime: float | None = None,
    ) -> dict:
        """Launch `argv` in its own process group and return its record at once."""
        with self._lock:
            running = [r for r in self.list(user_id) if r["state"] == RUNNING]
            if len([r for r in running if r.get("chat_id") == chat_id]) >= self.max_per_chat:
                raise ProcessError(f"limit: {self.max_per_chat} running processes in this chat; stop or wait for one first")
            if len(running) >= self.max_per_user:
                raise ProcessError(f"limit: {self.max_per_user} running processes for this user; stop or wait for one first")
            process_id = "p" + uuid.uuid4().hex[:8]
            kwargs: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            try:
                proc = subprocess.Popen(
                    argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs,
                )
            except (OSError, ValueError) as exc:
                raise ProcessError(f"could not start the process: {exc}") from None
            record = {
                "id": process_id, "user": user_id, "agent": agent_id, "chat_id": chat_id, "backend": backend,
                "command": command, "cwd": cwd, "started": time.time(), "state": RUNNING, "exit_code": None,
                "ended": None, "pid": proc.pid, "notify": bool(notify), "kind": "local",
                "max_lifetime": float(max_lifetime if max_lifetime is not None else self.max_lifetime),
            }
            self._save(record)
            live = _Live(proc=proc)
            self._live[(user_id, process_id)] = live
        live.reader = threading.Thread(target=self._pump, args=(record, live), daemon=True, name=f"proc-{process_id}-out")
        live.reader.start()
        threading.Thread(target=self._watch, args=(record, live), daemon=True, name=f"proc-{process_id}-watch").start()
        return dict(record)

    def _pump(self, record: dict, live: _Live) -> None:
        """Copy the process output into its log, keeping the last `log_cap` bytes."""
        path = self.log_path(record["user"], record["id"])
        path.write_bytes(b"")
        size = 0
        try:
            fd = live.proc.stdout.fileno()
            while chunk := os.read(fd, _CHUNK):
                with open(path, "ab") as handle:
                    handle.write(chunk)
                size += len(chunk)
                if size > 2 * self.log_cap:
                    data = path.read_bytes()[-self.log_cap:]
                    path.write_bytes(data)
                    size = len(data)
        except (OSError, ValueError):
            pass

    def _watch(self, record: dict, live: _Live) -> None:
        deadline = record["started"] + record["max_lifetime"]
        proc = live.proc
        while True:
            try:
                code = proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if time.time() >= deadline and not live.stop_reason:
                    live.stop_reason = "timed_out"
                    kill_tree(proc)
        if live.reader is not None:  # let the reader drain the pipe before the record says finished
            live.reader.join(timeout=5)
        try:
            proc.stdout.close()
        except OSError:
            pass
        with self._lock:
            try:
                current = self.get(record["user"], record["id"])
            except ProcessError:  # the record is gone (its folder was removed): nothing left to update
                self._live.pop((record["user"], record["id"]), None)
                live.done.set()
                return
            current["state"] = live.stop_reason or "exited"
            current["exit_code"] = code if not live.stop_reason else None
            current["ended"] = time.time()
            self._save(current)
            self._live.pop((record["user"], record["id"]), None)
        live.done.set()
        for callback in list(self._on_finish):
            try:
                callback(dict(current))
            except Exception:  # a hook must never break the watcher
                logger.exception("process finish hook failed")

    # ---- use
    def read(self, user_id: str, process_id: str, tail: int = 50) -> dict:
        """The record plus the last `tail` lines of its output."""
        record = self.get(user_id, process_id)
        try:
            text = self.log_path(user_id, process_id).read_bytes().decode("utf-8", "replace")
        except OSError:
            text = ""
        tail = max(1, min(int(tail), MAX_TAIL_LINES))
        lines = text.splitlines()
        record["output"] = "\n".join(lines[-tail:])
        record["output_lines"] = len(lines)
        record["output_truncated"] = len(lines) > tail
        return record

    def wait(self, user_id: str, process_id: str, timeout_s: float = 30.0, cancelled=None) -> dict:
        """Block until the process ends or `timeout_s` (at most 120 s) passes; returns the record either way."""
        timeout_s = max(0.0, min(float(timeout_s), 120.0))
        live = self._live.get((user_id, process_id))
        deadline = time.monotonic() + timeout_s
        while live is not None and not live.done.is_set():
            if time.monotonic() >= deadline or (cancelled is not None and cancelled()):
                break
            live.done.wait(0.2)
        return self.get(user_id, process_id)

    def stop(self, user_id: str, process_id: str) -> dict:
        record = self.get(user_id, process_id)
        if record["state"] != RUNNING:
            raise ProcessError(f"process {process_id} is not running (state: {record['state']})")
        live = self._live.get((user_id, process_id))
        if live is None or live.proc is None:
            raise ProcessError(f"process {process_id} is not held by this server; it cannot be stopped from here")
        live.stop_reason = live.stop_reason or "stopped"
        kill_tree(live.proc)
        live.done.wait(10)
        return self.get(user_id, process_id)

    def stop_chat(self, user_id: str, chat_id: str) -> int:
        """Stop every running process of a chat (chat deletion)."""
        count = 0
        for record in self.list(user_id, chat_id):
            if record["state"] == RUNNING:
                try:
                    self.stop(user_id, record["id"])
                    count += 1
                except ProcessError:
                    pass
        return count

    def recover(self, user_id: str) -> int:
        """After a restart: records still `running` that this server does not hold are `lost`."""
        count = 0
        with self._lock:
            for record in self.list(user_id):
                if record["state"] == RUNNING and record.get("kind", "local") == "local" and (user_id, record["id"]) not in self._live:
                    record["state"] = "lost"
                    record["ended"] = time.time()
                    self._save(record)
                    count += 1
        return count
