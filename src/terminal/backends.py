"""Command backends: where terminal commands actually run (an SSH sandbox, or the local host)."""

from __future__ import annotations

import os
import posixpath
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.config.models import LocalBackendConfig, SshBackendConfig
from src.providers.cancellation import CancellationToken
from src.terminal.context import current_run
from src.utils.logger import get_logger

logger = get_logger("terminal")

_CONNECT_TIMEOUT = 10
_CANCEL_POLL_SECONDS = 0.2  # how often a running command looks at the stop signal
# the ONLY environment variables a local process inherits (an allowlist: SSH_AUTH_SOCK, DOCKER_HOST, KUBECONFIG,
# DBUS_SESSION_BUS_ADDRESS, DATABASE_URL, proxies, tokens and every RECALL setting are never passed)
_ALLOWED_ENV = ("PATH", "HOME", "LANG", "TERM", "TMPDIR", "USER", "LOGNAME", "SHELL", "TZ")
_ALLOWED_ENV_PREFIXES = ("LC_",)
_WINDOWS_ENV = ("USERPROFILE", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC")
# what the local `ssh` process needs and nothing else
_SAFE_ENV = ("PATH", "HOME", "USERPROFILE", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL", "COMSPEC")


@dataclass
class Result:
    """Outcome of one command. `exit_code` is None when it never ran or was killed on timeout."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False
    cancelled: bool = False  # the user stopped the run while it was executing: the process tree was killed
    approval: str = ""  # one line telling the agent how the command was authorized (set by TerminalSession.run)


class Backend(Protocol):
    name: str

    def run(self, command: str, cwd: str, timeout: float, stdin: bytes | str | None = None,
            cancellation: CancellationToken | None = None) -> Result: ...


def scrubbed_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a local process: an allowlist of the parent's variables, nothing else."""
    source = environ if environ is not None else os.environ
    allowed = _ALLOWED_ENV + (_WINDOWS_ENV if os.name == "nt" else ())
    return {k: v for k, v in source.items() if k in allowed or k.startswith(_ALLOWED_ENV_PREFIXES)}


def minimal_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """What the local `ssh` process needs and nothing else (no SSH agent, no proxy, no tokens)."""
    source = environ if environ is not None else os.environ
    return {name: source[name] for name in _SAFE_ENV if name in source}


def remote_cd(cwd: str) -> str:
    """`cd <cwd>` for a POSIX shell; a leading `~` expands to $HOME (a quoted `~` would not)."""
    if cwd == "~":
        return 'cd "$HOME"'
    if cwd.startswith("~/"):
        return 'cd "$HOME"/' + shlex.quote(cwd[2:])
    return "cd " + shlex.quote(cwd)


def remote_command(command: str, cwd: str) -> str:
    """The single string handed to the remote login shell: `bash -lc '<cd && command>'`, fully quoted."""
    return "bash -lc " + shlex.quote(f"{remote_cd(cwd)} && {command}")


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill a process and its children."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def run_process(argv: list[str], *, cwd: str | None, env: dict[str, str], timeout: float,
                stdin: bytes | str | None, cancellation: CancellationToken | None = None) -> Result:
    """Run `argv` (never through a local shell) with a hard timeout that kills the whole process tree.

    The stop signal (`cancellation`, default: the current run's) is polled in short slices: when it fires the tree is
    killed at once and the Result is marked `cancelled`."""
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    if os.name == "nt":
        kwargs: dict = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    else:
        kwargs = {"start_new_session": True}
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs,
        )
    except (OSError, ValueError) as exc:
        return Result(None, stderr=f"could not start the process: {exc}")
    if cancellation is None:
        run = current_run()
        cancellation = run.cancellation if run is not None else None
    deadline = time.monotonic() + timeout
    pending: bytes | None = data  # handed to communicate() once; later slices only wait
    timed_out = cancelled = False
    while True:
        remaining = deadline - time.monotonic()
        if cancellation is not None and cancellation.is_cancelled():
            cancelled = True
        elif remaining <= 0:
            timed_out = True
        if cancelled or timed_out:
            kill_tree(proc)
            break
        try:
            out, err = proc.communicate(pending, timeout=min(_CANCEL_POLL_SECONDS, remaining))
            break
        except subprocess.TimeoutExpired:
            pending = None
    if cancelled or timed_out:
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.SubprocessError:
            out, err = b"", b""
    return Result(
        None if timed_out or cancelled else proc.returncode,
        (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"), timed_out,
        cancelled=cancelled,
    )


class SshBackend:
    """A sandbox reached with the `ssh` binary: key-only, strict host-key checking, nothing forwarded."""

    def __init__(self, name: str, config: SshBackendConfig, *, ssh_binary: str | None = None):
        self.name = name
        self.config = config
        self.ssh_binary = ssh_binary or shutil.which("ssh") or "ssh"

    def argv(self, command: str, cwd: str) -> list[str]:
        cfg = self.config
        return [
            self.ssh_binary,
            "-F", os.devnull,  # never read the RECALL user's ~/.ssh/config (ProxyCommand, Include, Match exec...)
            "-i", str(Path(cfg.key_file).expanduser()),
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={Path(cfg.known_hosts_file).expanduser()}",
            "-o", f"ConnectTimeout={_CONNECT_TIMEOUT}",
            "-o", "ForwardAgent=no",
            "-o", "ForwardX11=no",
            "-o", "LogLevel=ERROR",
            "-p", str(cfg.port),
            f"{cfg.user}@{cfg.host}",
            "--",
            remote_command(command, cwd),
        ]

    @staticmethod
    def pid_file(process_id: str) -> str:
        return f"/tmp/recall-proc-{process_id}.pid"

    def process_argv(self, command: str, cwd: str, process_id: str) -> list[str]:
        """ssh argv that runs `command` in its own remote session, its leader's pid in a file so it can be killed later."""
        pid_file = self.pid_file(process_id)
        script = f"echo $$ > {pid_file}; {remote_command(command, cwd)}; code=$?; rm -f {pid_file}; exit $code"
        argv = self.argv("true", "~")[:-1]
        return [*argv, f"setsid -w bash -c {shlex.quote(script)}"]

    def kill_remote(self, process_id: str) -> Result:
        """Kill the remote process group started by `process_argv` (the local ssh is killed by the caller)."""
        pid_file = self.pid_file(process_id)
        return self.run(f"kill -KILL -- -$(cat {pid_file}) 2>/dev/null; rm -f {pid_file}", "~", 15)

    def run(self, command: str, cwd: str, timeout: float, stdin: bytes | str | None = None,
            cancellation: CancellationToken | None = None) -> Result:
        result = run_process(
            self.argv(command, cwd), cwd=None, env=minimal_env(), timeout=timeout + _CONNECT_TIMEOUT, stdin=stdin,
            cancellation=cancellation,
        )
        if result.exit_code == 255 and not result.timed_out and not result.cancelled:  # ssh itself failed (connection, key, host key)
            result.stderr = (result.stderr.strip() + "\n[ssh could not reach or authenticate to the sandbox]").strip()
        return result


class LocalBackend:
    """Commands on the RECALL host in a working directory, with a scrubbed environment (trusted installs only)."""

    def __init__(self, name: str, config: LocalBackendConfig):
        self.name = name
        self.config = config

    @staticmethod
    def shell() -> list[str]:
        bash = shutil.which("bash")
        if bash:
            return [bash, "-c"]
        if os.name == "nt":
            return [os.environ.get("COMSPEC", "cmd.exe"), "/c"]
        return ["/bin/sh", "-c"]

    def run(self, command: str, cwd: str, timeout: float, stdin: bytes | str | None = None,
            cancellation: CancellationToken | None = None) -> Result:
        base = Path(self.config.workdir).expanduser()
        target = Path(cwd).expanduser() if cwd and cwd != "~/workspace" else base
        if not target.is_absolute():
            target = base / target
        if not target.is_dir():
            return Result(None, stderr=f"working directory does not exist: {target}")
        return run_process([*self.shell(), command], cwd=str(target), env=scrubbed_env(), timeout=timeout, stdin=stdin,
                           cancellation=cancellation)


def build_backend(name: str, config: SshBackendConfig | LocalBackendConfig) -> Backend:
    return SshBackend(name, config) if config.type == "ssh" else LocalBackend(name, config)


def join_path(base: str, path: str) -> str:
    """`path` inside the sandbox: absolute and `~` paths stay, relative ones hang from `base`."""
    if path.startswith(("/", "~")):
        return path
    return posixpath.join(base, path)
