"""Sign in to a Claude subscription from the web UI: drives `claude auth login` for one user's own login folder.

The CLI prints an OAuth URL, then waits for the code the browser shows. This keeps that one process per user
between two web requests (start, then code). The URL and the code are never logged, and RECALL never reads the
credential files: success is decided by `claude auth status` (see status.cli_status).
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from src.config.models import ClaudeSubscriptionConfig
from src.providers.claude_subscription import native
from src.providers.claude_subscription.login import command_for
from src.providers.claude_subscription.status import cli_status
from src.utils.i18n import InputError

URL_WAIT = 20.0         # seconds to wait for the CLI to print the URL
CODE_WAIT = 30.0        # seconds to wait for the CLI to finish after the code
SESSION_TTL = 300.0     # a started login that gets no code is killed after this
_URL = re.compile(r"https://[^\s]+/oauth/authorize\?[^\s]+")
_CODE = re.compile(r"^[A-Za-z0-9_#.~-]{8,512}$")
_FAILED = re.compile(r"Login failed:?\s*(.*)")


@dataclass
class _Session:
    process: subprocess.Popen
    lines: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def text(self) -> str:
        with self.lock:
            return "\n".join(self.lines)


_sessions: dict[str, _Session] = {}
_registry_lock = threading.Lock()


def _reader(session: _Session) -> None:
    for line in session.process.stdout:  # ends when the process closes its output
        with session.lock:
            session.lines.append(line.rstrip("\n"))


def _drop(user_id: str) -> None:
    with _registry_lock:
        session = _sessions.pop(user_id, None)
    if session is not None:
        native.kill_process_tree(session.process)


def _expire(user_id: str, session: _Session) -> None:
    with _registry_lock:
        if _sessions.get(user_id) is not session:
            return
    _drop(user_id)


def cancel(user_id: str) -> None:
    _drop(user_id)


def start(user_id: str, claude: ClaudeSubscriptionConfig) -> dict:
    """Start a login for `claude` (already resolved to the user's own folder); returns {"url": ...}."""
    _drop(user_id)
    env = native.child_env(dict(os.environ), config_dir=claude.claude_config_dir, telemetry=claude.claude_code_telemetry)
    command = native.resolve_command(command_for(claude), env)
    if command is None:
        raise InputError("Claude Code is not installed on the computer running RECALL.", code="claude_not_installed")
    os.makedirs(env["CLAUDE_CONFIG_DIR"], exist_ok=True)
    try:
        process = subprocess.Popen(
            [*command, "auth", "login", "--claudeai"], env=env, cwd=native.workdir(), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            **native.own_process_group(),
        )
    except OSError as exc:
        raise InputError("Claude Code could not be started: {error}", code="claude_start_failed", error=type(exc).__name__)
    session = _Session(process)
    with _registry_lock:
        _sessions[user_id] = session
    threading.Thread(target=_reader, args=(session,), daemon=True).start()
    timer = threading.Timer(SESSION_TTL, _expire, args=(user_id, session))
    timer.daemon = True
    timer.start()
    deadline = time.monotonic() + URL_WAIT
    while time.monotonic() < deadline:
        found = _URL.search(session.text())
        if found:
            return {"url": found.group(0)}
        if process.poll() is not None:
            break
        time.sleep(0.1)
    _drop(user_id)
    raise InputError("Claude Code did not give a sign-in link. Try again.", code="claude_login_no_url")


def submit(user_id: str, code: str, claude: ClaudeSubscriptionConfig) -> dict:
    """Give the pasted code to the waiting login; returns the new status or raises the CLI's refusal."""
    code = (code or "").strip()
    if not _CODE.match(code):
        raise InputError("That does not look like a Claude sign-in code.", code="claude_login_bad_code")
    with _registry_lock:
        session = _sessions.get(user_id)
    if session is None or session.process.poll() is not None:
        _drop(user_id)
        raise InputError("There is no sign-in waiting for a code. Start it again.", code="claude_login_not_started")
    try:
        session.process.stdin.write(code + "\n")
        session.process.stdin.flush()
    except OSError:
        _drop(user_id)
        raise InputError("There is no sign-in waiting for a code. Start it again.", code="claude_login_not_started")
    try:
        session.process.wait(timeout=CODE_WAIT)
    except subprocess.TimeoutExpired:
        _drop(user_id)
        raise InputError("Claude did not answer in time. Start the sign-in again.", code="claude_login_timeout")
    time.sleep(0.2)  # let the reader thread take the last line
    failed = _FAILED.search(session.text())
    with _registry_lock:
        _sessions.pop(user_id, None)
    status = cli_status(claude)
    if failed or not status.get("logged_in"):
        raise InputError("Claude refused the code. Start the sign-in again to get a new one.", code="claude_login_refused")
    return status
