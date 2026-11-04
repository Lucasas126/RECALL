"""Fakes shared by the terminal tests: no ssh, no network, no real shell."""

from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from types import SimpleNamespace

from src.config.models import AgentTerminalConfig, ModelsConfig, ResolvedAgent, SshBackendConfig, TerminalSettings
from src.providers.cancellation import CancellationToken
from src.terminal.approvals import ApprovalBroker, Outcome
from src.terminal.backends import Result
from src.terminal.context import RunContext
from src.terminal.session import TerminalSession


def ssh_config(**changes) -> SshBackendConfig:
    values = dict(type="ssh", host="192.0.2.10", port=22, user="agent", key_file="/keys/id_test",
                  known_hosts_file="/keys/known_hosts")
    return SshBackendConfig(**{**values, **changes})


def resolved_agent(approvals: str = "manual", **terminal) -> ResolvedAgent:
    return ResolvedAgent(
        user_id="u", id="a", name="A", models=ModelsConfig(), provider_name="ollama", api_key="",
        embeddings_provider_name="ollama", embeddings_api_key="", tools=True, tool_groups=["terminal"],
        terminal=AgentTerminalConfig(backend="box", approvals=approvals, **terminal), terminal_backend=ssh_config(),
    )


class FakeBackend:
    """Records every call and answers with `handler(command, cwd, timeout, stdin)` (default: exit 0, 'ok')."""

    name = "fake"

    def __init__(self, handler=None):
        self.calls: list[SimpleNamespace] = []
        self.handler = handler or (lambda command, cwd, timeout, stdin: Result(0, "ok\n"))

    def run(self, command, cwd, timeout, stdin=None):
        self.calls.append(SimpleNamespace(command=command, cwd=cwd, timeout=timeout, stdin=stdin))
        return self.handler(command, cwd, timeout, stdin)


class FakeSandbox(FakeBackend):
    """An in-memory filesystem that understands the shell snippets of src/terminal/files.py."""

    def __init__(self, files: dict[str, str] | None = None):
        super().__init__(self._serve)
        self.files = dict(files or {})

    @staticmethod
    def _path(command: str) -> str:
        value = command.split("\n", 1)[0].removeprefix("f=")
        if value.startswith('"$HOME"/'):
            return "~/" + shlex.split(value.removeprefix('"$HOME"/'))[0]
        return shlex.split(value)[0]

    def _serve(self, command, cwd, timeout, stdin):
        path = self._path(command)
        if "mktemp" in command:  # write
            self.files[path] = (stdin or b"").decode("utf-8")
            return Result(0)
        if path not in self.files and "find " not in command and "grep -rIn" not in command:
            return Result(3)
        if "awk -v s=" in command:  # read
            start, end = (int(n) for n in re.search(r"-v s=(\d+) -v e=(\d+)", command).groups())
            lines = self.files[path].splitlines()
            shown = [f"{i:6d}|{line}" for i, line in enumerate(lines, 1) if start <= i <= end]
            return Result(0, "\n".join([*shown, f"@@TOTAL {len(lines)}"]) + "\n")
        if "cat -- " in command:  # fetch for patch
            return Result(0, self.files[path])
        tokens = shlex.split(command.split("\n", 2)[2])
        if "-name" in tokens:
            glob = tokens[tokens.index("-name") + 1]
            hits = [p for p in self.files if fnmatch.fnmatch(posixpath.basename(p), glob)]
            return Result(0, "\n".join(hits) + ("\n" if hits else ""))
        pattern = tokens[tokens.index("-e") + 1]
        include = next((t.split("=", 1)[1] for t in tokens if t.startswith("--include=")), None)
        hits = [
            f"{p}:{n}:{line}" for p, text in self.files.items()
            if include is None or fnmatch.fnmatch(posixpath.basename(p), include)
            for n, line in enumerate(text.splitlines(), 1) if re.search(pattern, line)
        ]
        return Result(0, "\n".join(hits) + ("\n" if hits else ""))


class Responder:
    """A chat run for tests: collects events and answers approvals at once (`answer`) or leaves them pending."""

    def __init__(self, broker: ApprovalBroker, answer: str | None = "approve", user: str = "u"):
        self.broker, self.answer, self.user = broker, answer, user
        self.events: list[tuple[str, dict]] = []
        self.cancellation = CancellationToken()

    def emit(self, kind: str, data: dict) -> None:
        self.events.append((kind, data))
        if kind == "approval" and self.answer:
            self.broker.resolve(data["id"], self.user, self.answer)

    def context(self, *, interactive: bool = True) -> RunContext:
        return RunContext("u", "a", "chat1", self.broker, self.emit, self.cancellation, interactive=interactive)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]


class StubBroker:
    """Records requests and answers with a fixed outcome."""

    def __init__(self, approved: bool = True, reason: str | None = None):
        self.requests: list[dict] = []
        self.outcome = Outcome(approved, reason or ("approved" if approved else "denied by user"), by_user=True)

    def request(self, user_id, agent_id, chat_id, run, tool, command, cwd, reason, risk, **kwargs):
        self.requests.append({"tool": tool, "command": command, "cwd": cwd, "reason": reason, "risk": risk})
        return self.outcome


def make_session(backend=None, approvals: str = "manual", *, unattended: bool = False, audit_file=None,
                 **terminal) -> TerminalSession:
    return TerminalSession(
        backend or FakeBackend(), AgentTerminalConfig(backend="box", approvals=approvals, **terminal),
        TerminalSettings(), user_id="u", agent_id="a", audit_file=audit_file, unattended=unattended,
    )
