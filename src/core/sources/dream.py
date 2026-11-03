from __future__ import annotations

import json
import re
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from src.core.sources.base import Source
from src.utils.files import NonBlockingFileLock, atomic_write_text, exclusive_file_lock
from src.utils.i18n import InputError

_SESSION_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
REVIEW_STATE_FILE = "review_state.json"
CANCEL_FILE = ".cancel"  # holds the id of a session another process asked to stop


class DreamStateSource(Source):
    """Dream mode bookkeeping of one agent: review queue state, session traces and the run lock.

    Not readable by the agent's generic tools; the dream runner and the service use the methods below.
    """

    def __init__(self, root: Path):
        super().__init__("dream", root, readable=False, writable=False, indexed=False)

    # ------------------------------------------------------------------ run lock

    def run_lock(self) -> NonBlockingFileLock:
        """The lock held while a session runs; shared by every process using this data folder."""
        return NonBlockingFileLock(self.resolve(".lock"))

    def is_running(self) -> bool:
        """Whether some process currently holds the run lock."""
        probe = self.run_lock()
        if probe.acquire():
            probe.release()
            return False
        return True

    # ------------------------------------------------------------------ cancel requests

    def request_cancel(self, session_id: str) -> None:
        """Ask the process running `session_id` to stop it; its runner polls this file."""
        self._session_path(session_id, ".jsonl")  # validates the id
        atomic_write_text(self.resolve(CANCEL_FILE), session_id + "\n")

    def pending_cancel(self) -> str | None:
        """The session id named by the cancel request file, or None when there is no request."""
        try:
            return self.resolve(CANCEL_FILE).read_text(encoding="utf-8").strip() or None
        except FileNotFoundError:
            return None

    def cancel_requested(self, session_id: str) -> bool:
        return self.pending_cancel() == session_id

    def clear_cancel(self, session_id: str | None = None) -> None:
        """Remove the cancel request (only when it names `session_id`, if given)."""
        if session_id is None or self.cancel_requested(session_id):
            self.resolve(CANCEL_FILE).unlink(missing_ok=True)

    # ------------------------------------------------------------------ review state

    def state(self) -> dict:
        """The review state: per-chat watermarks plus session timestamps."""
        path = self.resolve(REVIEW_STATE_FILE)
        if not path.is_file():
            return _empty_state()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not read {path}; fix or delete it to reset the review queue") from exc
        if not isinstance(data, dict) or not isinstance(data.get("chats", {}), dict):
            raise RuntimeError(f"invalid review state in {path}; fix or delete it to reset the review queue")
        return {**_empty_state(), **data}

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        with exclusive_file_lock(self.resolve(".state.lock")):
            yield

    def update_state(self, change: Callable[[dict], None]) -> dict:
        """Apply `change` to the review state under a cross-process lock and save it."""
        with self._state_lock():
            state = self.state()
            change(state)
            atomic_write_text(self.resolve(REVIEW_STATE_FILE), json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            return state

    # ------------------------------------------------------------------ sessions

    def new_session_id(self, now: datetime) -> str:
        return f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"

    def append_event(self, session_id: str, event: dict) -> None:
        """Append one trace event as a JSONL line, flushed at once so a crash keeps what happened."""
        path = self._session_path(session_id, ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            fh.flush()

    def write_summary(self, session_id: str, summary: dict) -> None:
        path = self._session_path(session_id, ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n")

    def summary(self, session_id: str) -> dict | None:
        """A finished session's summary; None while it runs (or if it stopped without one)."""
        path = self._session_path(session_id, ".json")
        if not path.is_file():
            if not self._session_path(session_id, ".jsonl").is_file():
                raise FileNotFoundError(f"dream session not found: {session_id}")
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def trace(self, session_id: str) -> list[dict]:
        path = self._session_path(session_id, ".jsonl")
        if not path.is_file():
            raise FileNotFoundError(f"dream session not found: {session_id}")
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def session_ids(self) -> list[str]:
        """Ids of every session with a trace, newest first."""
        folder = self.resolve("sessions")
        if not folder.is_dir():
            return []
        return sorted((p.stem for p in folder.glob("*.jsonl") if _SESSION_RE.match(p.stem)), reverse=True)

    def _session_path(self, session_id: str, suffix: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_RE.match(session_id):
            raise InputError("invalid dream session id: {session_id}", session_id=session_id)
        return self.resolve(f"sessions/{session_id}{suffix}")


def _empty_state() -> dict:
    return {"chats": {}, "last_started_at": None, "last_finished_at": None, "scheduled_since": None}
