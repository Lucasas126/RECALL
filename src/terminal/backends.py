"""Command backends: where terminal commands actually run (an SSH sandbox, or the local host)."""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from src.config.models import LocalBackendConfig, SshBackendConfig
from src.utils.logger import get_logger

logger = get_logger("terminal")

_CONNECT_TIMEOUT = 10
# environment variables that never reach a child process (secrets of the RECALL deployment)
_SECRET_NAME = re.compile(
    r"(^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?|COOKIE)($|_)"
    r"|^(RECALL|OPENROUTER|TAVILY|TELEGRAM|ANTHROPIC|OPENAI|GITHUB|GH)(_|$)"
    r"|SECRET|TOKEN|_KEY$|API_?KEY",
    re.I,
)
_SAFE_ENV = ("PATH", "HOME", "USERPROFILE", "SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL", "COMSPEC")


@dataclass
class Result:
    """Outcome of one command. `exit_code` is None when it never ran or was killed on timeout."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    truncated: bool = False


class Backend(Protocol):
    name: str

    def run(self, command: str, cwd: str, timeout: float, stdin: bytes | str | None = None) -> Result: ...


def scrubbed_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The parent environment without anything that looks like a secret or a RECALL setting."""
    source = environ if environ is not None else os.environ
    return {k: v for k, v in source.items() if not _SECRET_NAME.search(k)}


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
                stdin: bytes | str | None) -> Result:
    """Run `argv` (never through a local shell) with a hard timeout that kills the whole process tree."""
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
    try:
        out, err = proc.communicate(data, timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except subprocess.SubprocessError:
            out, err = b"", b""
        timed_out = True
    return Result(
        None if timed_out else proc.returncode,
        (out or b"").decode("utf-8", "replace"), (err or b"").decode("utf-8", "replace"), timed_out,
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

    def run(self, command: str, cwd: str, timeout: float, stdin: bytes | str | None = None) -> Result:
        result = run_process(
            self.argv(command, cwd), cwd=None, env=minimal_env(), timeout=timeout + _CONNECT_TIMEOUT, stdin=stdin,
        )
        if result.exit_code == 255 and not result.timed_out:  # ssh itself failed (connection, key, host key)
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

    def run(self, command: str, cwd: str, timeout: float, stdin: bytes | str | None = None) -> Result:
        base = Path(self.config.workdir).expanduser()
        target = Path(cwd).expanduser() if cwd and cwd != "~/workspace" else base
        if not target.is_absolute():
            target = base / target
        if not target.is_dir():
            return Result(None, stderr=f"working directory does not exist: {target}")
        return run_process([*self.shell(), command], cwd=str(target), env=scrubbed_env(), timeout=timeout, stdin=stdin)


def build_backend(name: str, config: SshBackendConfig | LocalBackendConfig) -> Backend:
    return SshBackend(name, config) if config.type == "ssh" else LocalBackend(name, config)


def join_path(base: str, path: str) -> str:
    """`path` inside the sandbox: absolute and `~` paths stay, relative ones hang from `base`."""
    if path.startswith(("/", "~")):
        return path
    return posixpath.join(base, path)
