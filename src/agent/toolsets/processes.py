"""The background-process tools of the terminal group: start a long command, then poll, wait for or stop it by id."""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Callable

from src.agent.tooling import tool
from src.terminal.audit import audit_path
from src.terminal.backends import LocalBackend, SshBackend, minimal_env, scrubbed_env
from src.terminal.context import current_run
from src.terminal.output import clean, redact
from src.terminal.processes import RUNNING, ProcessError, ProcessRegistry
from src.terminal.session import TerminalSession

_registries: dict[str, ProcessRegistry] = {}


def process_registry(data_dir) -> ProcessRegistry:
    """The one registry of this server process for a data folder (a restart marks what it cannot hold as lost)."""
    key = str(data_dir)
    if key not in _registries:
        _registries[key] = ProcessRegistry(data_dir)
    return _registries[key]


def _age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m" if seconds >= 3600 else f"{seconds // 60}m{seconds % 60:02d}s"


def _summary(record: dict) -> str:
    end = record.get("ended") or time.time()
    exit_part = "" if record["exit_code"] is None else f", exit code {record['exit_code']}"
    return (
        f"{record['id']} [{record['state']}{exit_part}] on {record['backend']}, running {_age(end - record['started'])}: "
        f"{redact(record['command'][:120])}"
    )


def build_process_tools(
    sessions: dict[str, TerminalSession], default: str, data_dir, *, agent_id: str, user_id: str,
) -> list[Callable[..., str]]:
    """`process_start`, `process_list`, `process_read`, `process_wait` and `process_stop` over the agent's backends."""
    registry = process_registry(data_dir)
    registry.recover(user_id)
    names = list(sessions)

    def mine(process_id: str) -> tuple[dict | None, str | None]:
        try:
            record = registry.get(user_id, process_id)
        except ProcessError as exc:
            return None, f"error: {exc}"
        if record.get("agent") != agent_id:  # another agent's process is not this agent's to read or stop
            return None, f"error: unknown process: {process_id!r}"
        return record, None

    @tool
    def process_start(
        command: Annotated[str, "shell command to run in the background; non-interactive, nothing is typed into it"],
        workdir: Annotated[str | None, "working directory on the backend (default: the backend's workdir)"] = None,
        backend: Annotated[str | None, "which backend (machine) to use; omit for the default"] = None,
        notify_on_complete: Annotated[bool, "wake this chat with a message when the process ends (default true)"] = True,
    ) -> str:
        """Start a long-running command in the background and return its id at once (builds, test suites, servers,
        downloads). The same approval rules as `terminal` apply to the start. Then use `process_read` for the output,
        `process_wait` to block briefly, `process_stop` to end it. A process ends on its own after the maximum
        lifetime. Its output keeps only the last ~200 KB."""
        chosen = (backend or "").strip() or default
        session = sessions.get(chosen)
        if session is None:
            return f"error: unknown backend '{chosen}'; allowed backends: {', '.join(names)} (default: {default})"
        ssh = isinstance(session.backend, SshBackend)
        if not ssh and not isinstance(session.backend, LocalBackend):
            return "error: background processes are not available on this backend; use `terminal` with a timeout"
        allowed, denial = session.authorize("process_start", command, command, cwd=workdir or None)
        if denial is not None:
            return denial
        cwd, ruling, verdict, decision = allowed
        run = current_run()
        started = time.monotonic()
        process_id = "p" + uuid.uuid4().hex[:8]
        if ssh:
            where = dict(
                argv=session.backend.process_argv(command, cwd, process_id), cwd=None, env=minimal_env(), kind="ssh",
                remote_stop=session.backend.kill_remote,
            )
        else:
            where = dict(argv=[*LocalBackend.shell(), command], cwd=session.local_cwd(cwd), env=scrubbed_env())
        try:
            record = registry.start_local(
                user_id=user_id, agent_id=agent_id, chat_id=run.chat_id if run else "", backend=chosen, command=command,
                notify=notify_on_complete, process_id=process_id, **where,
            )
        except ProcessError as exc:
            session._audit("process_start", command, cwd, decision, None, time.monotonic() - started, verdict=verdict.level, ruling=ruling)
            return f"error: {exc}"
        session._audit("process_start", command, cwd, decision, None, time.monotonic() - started, verdict=verdict.level, ruling=ruling)
        note = session.approval_note(decision, ruling)
        wake = "You will be woken with a message when it ends." if notify_on_complete else "No message will wake you; poll it."
        return "\n".join(filter(None, [note, f"started process {record['id']} on {chosen}. {wake}"]))

    @tool
    def process_list(
        running_only: Annotated[bool, "only the processes still running"] = False,
    ) -> str:
        """List your background processes (this agent's, newest last) with state and exit code."""
        records = [r for r in registry.list(user_id) if r.get("agent") == agent_id and (not running_only or r["state"] == RUNNING)]
        return "\n".join(_summary(r) for r in records[-30:]) or "no background processes"

    @tool
    def process_read(
        id: Annotated[str, "process id from process_start"],
        tail: Annotated[int, "how many of the last output lines to show (default 50, at most 500)"] = 50,
    ) -> str:
        """The state, exit code and last output lines of a background process (stdout and stderr together)."""
        record, error = mine(id)
        if record is None:
            return error
        data = registry.read(user_id, id, tail)
        out, _ = clean(data["output"], 20_000)
        cut = f"\n(showing the last {tail} of {data['output_lines']} lines)" if data["output_truncated"] else ""
        return f"{_summary(data)}{cut}\n" + (out if out.strip() else "(no output yet)")

    @tool
    def process_wait(
        id: Annotated[str, "process id from process_start"],
        timeout_s: Annotated[int, "seconds to wait for it to end (default 30, at most 120)"] = 30,
    ) -> str:
        """Wait until a background process ends or the timeout passes, then give its state and last output."""
        record, error = mine(id)
        if record is None:
            return error
        run = current_run()
        cancel = run.cancellation.is_cancelled if run is not None and run.cancellation is not None else None
        registry.wait(user_id, id, timeout_s, cancelled=cancel)
        data = registry.read(user_id, id, 30)
        out, _ = clean(data["output"], 20_000)
        state = "" if data["state"] != RUNNING else f"\nstill running after {timeout_s}s"
        return f"{_summary(data)}{state}\n" + (out if out.strip() else "(no output yet)")

    @tool
    def process_stop(id: Annotated[str, "process id from process_start"]) -> str:
        """Stop a running background process and everything it started."""
        record, error = mine(id)
        if record is None:
            return error
        try:
            data = registry.stop(user_id, id)
        except ProcessError as exc:
            return f"error: {exc}"
        return _summary(data)

    return [process_start, process_list, process_read, process_wait, process_stop]
