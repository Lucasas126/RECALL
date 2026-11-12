"""Background processes, sub-step (b): the five tools, their approvals, audit and isolation between agents."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

from terminal_helpers import FakeBackend, Responder, StubBroker, ssh_config
from src.agent.toolsets import processes as process_tools
from src.agent.toolsets.processes import build_process_tools
from src.config.models import AgentTerminalBackend, AgentTerminalConfig, LocalBackendConfig, TerminalSettings
from src.terminal.audit import audit_path
from src.terminal.backends import LocalBackend, SshBackend
from src.terminal.context import RunContext, run_context
from src.terminal.session import TerminalSession


class ToolsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        (self.root / "work").mkdir()
        process_tools._registries.clear()
        self.addCleanup(process_tools._registries.clear)

    def tools(self, approvals: str = "off", agent: str = "a", *, backend=None, name: str = "box") -> dict:
        backend = backend or LocalBackend(name, LocalBackendConfig(type="local", workdir=str(self.root / "work")))
        session = TerminalSession(
            backend, AgentTerminalConfig(backends={name: AgentTerminalBackend(approvals=approvals, workdir=str(self.root / "work"))}),
            TerminalSettings(), user_id="u", agent_id=agent, name=name, audit_file=audit_path(self.root, "u", agent),
            local=isinstance(backend, LocalBackend),
        )
        built = build_process_tools({name: session}, name, self.root, agent_id=agent, user_id="u")
        out = {tool.__name__: tool for tool in built}
        self.addCleanup(lambda: [process_tools.process_registry(self.root).stop_chat("u", chat) for chat in ("chat1", "")])
        return out


class ProcessToolTests(ToolsBase):
    def test_the_five_tools_exist(self) -> None:
        self.assertEqual(set(self.tools()), {"process_start", "process_list", "process_read", "process_wait", "process_stop"})

    def test_start_read_wait_and_exit_code(self) -> None:
        tools = self.tools()
        started = tools["process_start"](f"{sys.executable} -c \"print('hi'); import sys; sys.exit(2)\"")
        self.assertIn("started process p", started)
        process_id = started.split("started process ")[1].split(" ")[0]
        waited = tools["process_wait"](process_id, 20)
        self.assertIn("exit code 2", waited)
        self.assertIn("hi", waited)
        self.assertIn("hi", tools["process_read"](process_id))
        self.assertIn(process_id, tools["process_list"]())

    def test_stop_ends_a_running_process(self) -> None:
        tools = self.tools()
        process_id = tools["process_start"]("sleep 60").split("started process ")[1].split(" ")[0]
        self.assertIn("[running", tools["process_list"](running_only=True))
        self.assertIn("stopped", tools["process_stop"](process_id))
        self.assertEqual(tools["process_list"](running_only=True), "no background processes")
        self.assertIn("is not running", tools["process_stop"](process_id))

    def test_the_start_is_audited_with_its_decision(self) -> None:
        tools = self.tools()
        tools["process_start"]("echo audited")
        records = [json.loads(line) for line in audit_path(self.root, "u", "a").read_text().splitlines()]
        self.assertEqual([(r["tool"], r["command"], r["decision"]) for r in records], [("process_start", "echo audited", "auto:off")])

    def test_manual_mode_asks_and_a_denial_starts_nothing(self) -> None:
        tools = self.tools("manual")
        broker = StubBroker(approved=False)
        run = RunContext("u", "a", "chat1", broker, lambda *a: None, interactive=True)
        with run_context(run):
            reply = tools["process_start"]("echo nope")
        self.assertIn("Denied", reply)
        self.assertEqual([r["tool"] for r in broker.requests], ["process_start"])
        self.assertEqual(tools["process_list"](), "no background processes")

    def test_manual_mode_approved_starts_it(self) -> None:
        tools = self.tools("manual")
        run = RunContext("u", "a", "chat1", StubBroker(approved=True), lambda *a: None, interactive=True)
        with run_context(run):
            reply = tools["process_start"]("echo yes")
        self.assertIn("[approved by the user]", reply)
        self.assertIn("started process", reply)

    def test_hardline_and_unattended_never_start(self) -> None:
        tools = self.tools("off")
        self.assertIn("Blocked", tools["process_start"]("rm -rf /"))
        tools = self.tools("manual")
        self.assertIn("Denied", tools["process_start"]("echo x"))  # no run context: nobody to ask
        self.assertEqual(tools["process_list"](), "no background processes")

    def test_protected_paths_are_refused(self) -> None:
        secret = self.root / ".env"
        secret.write_text("TOKEN=x")
        reply = self.tools()["process_start"](f"cat {secret}")
        self.assertIn("protected path", reply)

    def test_another_agents_process_is_unknown(self) -> None:
        mine = self.tools(agent="a")
        process_id = mine["process_start"]("sleep 30").split("started process ")[1].split(" ")[0]
        other = self.tools(agent="b")
        self.assertIn("unknown process", other["process_read"](process_id))
        self.assertIn("unknown process", other["process_stop"](process_id))
        self.assertEqual(other["process_list"](), "no background processes")
        mine["process_stop"](process_id)

    def test_an_unknown_backend_kind_is_refused(self) -> None:
        tools = self.tools(backend=FakeBackend())
        self.assertIn("not available on this backend", tools["process_start"]("sleep 1"))

    def test_unknown_ids_and_the_limit_are_plain_errors(self) -> None:
        tools = self.tools()
        self.assertIn("unknown process", tools["process_read"]("p00000000"))
        self.assertIn("unknown process", tools["process_wait"]("nope"))
        replies = [tools["process_start"]("sleep 30") for _ in range(6)]
        self.assertTrue(replies[-1].startswith("error: limit"), replies[-1])

    def test_output_is_redacted(self) -> None:
        tools = self.tools()
        process_id = tools["process_start"]("echo sk-ant-abcdefghijkl123456").split("started process ")[1].split(" ")[0]
        self.assertNotIn("abcdefghijkl", tools["process_wait"](process_id, 20))


class SshProcessTests(ToolsBase):
    """The same tools over ssh, with a fake `ssh` that runs the remote command on this machine."""

    def fake_ssh(self) -> SshBackend:
        script = self.root / "ssh"
        script.write_text('#!/bin/bash\nfor last; do :; done\nexec bash -c "$last"\n')
        script.chmod(0o755)
        return SshBackend("box", ssh_config(), ssh_binary=str(script))

    def test_start_wait_and_exit_code_over_ssh(self) -> None:
        tools = self.tools(backend=self.fake_ssh())
        started = tools["process_start"]("echo remote; exit 3")
        process_id = started.split("started process ")[1].split(" ")[0]
        waited = tools["process_wait"](process_id, 20)
        self.assertIn("exit code 3", waited)
        self.assertIn("remote", waited)
        self.assertIn('"kind": "ssh"', (process_tools.process_registry(self.root).folder("u") / f"{process_id}.json").read_text())

    def test_stop_kills_the_remote_group_too(self) -> None:
        import os, time
        tools = self.tools(backend=self.fake_ssh())
        marker = self.root / "marker"
        process_id = tools["process_start"](f"echo $$ > {marker}; sleep 60").split("started process ")[1].split(" ")[0]
        for _ in range(50):
            if marker.exists() and marker.read_text().strip():
                break
            time.sleep(0.1)
        pid = int(marker.read_text())
        self.assertIn("stopped", tools["process_stop"](process_id))
        time.sleep(0.3)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)
        self.assertFalse(Path(SshBackend.pid_file(process_id)).exists())

    def test_argv_uses_the_hardened_ssh_options(self) -> None:
        argv = SshBackend("box", ssh_config(), ssh_binary="ssh").process_argv("make", "~/x", "pabc")
        self.assertEqual(argv[:3], ["ssh", "-F", "/dev/null"])
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("setsid -w bash -c", argv[-1])
        self.assertIn("/tmp/recall-proc-pabc.pid", argv[-1])


if __name__ == "__main__":
    unittest.main()
