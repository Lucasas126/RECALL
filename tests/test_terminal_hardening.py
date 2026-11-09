"""Terminal hardening: fail-closed defaults and what the local backend can reach (step 0A, item C)."""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

from src.terminal import files
from src.terminal import protected as protected_module
from src.terminal.approvals import ApprovalBroker
from src.terminal.audit import audit_writable, write_audit
from src.terminal.backends import SshBackend, scrubbed_env
from src.terminal.context import RunContext, run_context
from src.terminal.protected import build_protected
from src.terminal.session import AUDIT_FAILED
from terminal_helpers import FakeBackend, Responder, StubBroker, make_session, ssh_config
from test_terminal_protected import ProtectedCase, write

RM = "rm -rf build"


class OffModeUnattendedTests(unittest.TestCase):
    """(1) `off` must not widen what runs when nobody can answer."""

    def run_cmd(self, command, how, mode="off"):
        backend = FakeBackend()
        session = make_session(backend, mode, unattended=(how == "flag"))
        responder = Responder(ApprovalBroker(1), answer="approve")
        if how == "no-context":
            result, denial = session.run("terminal", command, command)
        else:
            with run_context(responder.context(interactive=(how != "non-interactive"))):
                result, denial = session.run("terminal", command, command)
        return backend, denial

    def test_unattended_off_refuses_dangerous_and_runs_normal(self) -> None:
        for how in ("flag", "no-context", "non-interactive"):
            with self.subTest(how=how):
                backend, denial = self.run_cmd(RM, how)
                self.assertIn("nobody is attached", denial)
                self.assertEqual(backend.calls, [])
                backend, denial = self.run_cmd("ls", how)
                self.assertIsNone(denial)
                self.assertEqual(len(backend.calls), 1)

    def test_unattended_off_refuses_file_writes_but_not_reads(self) -> None:
        for how in ("flag", "no-context"):
            with self.subTest(how=how):
                backend = FakeBackend()
                session = make_session(backend, "off", unattended=(how == "flag"))
                self.assertIn("Denied", files.sandbox_write(session, "a.txt", "x"))
                self.assertIn("Denied", files.sandbox_patch(session, "a.txt", "x", "y"))
                self.assertEqual(backend.calls, [])
                files.sandbox_read(session, "a.txt")
                self.assertEqual(len(backend.calls), 1)

    def test_hardline_still_wins_in_every_mode(self) -> None:
        for how in ("flag", "no-context"):
            backend, denial = self.run_cmd("rm -rf /", how)
            self.assertIn("hardline", denial)
            self.assertEqual(backend.calls, [])

    def test_attended_off_still_allows(self) -> None:
        backend = FakeBackend()
        session = make_session(backend, "off")
        with run_context(RunContext("u", "a", "c", None, lambda *a: None, interactive=True)):
            result, denial = session.run("terminal", RM, RM)
        self.assertIsNone(denial)
        self.assertEqual(len(backend.calls), 1)


class InteractiveDefaultTests(unittest.TestCase):
    """(2) `interactive` defaults to False; the chat path says True explicitly."""

    def test_default_is_false(self) -> None:
        self.assertFalse(RunContext("u", "a", "c", None, lambda *a: None).interactive)

    def test_chat_path_sets_it_explicitly(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "src" / "service" / "chat.py").read_text(encoding="utf-8")
        calls = re.findall(r"RunContext\((.*?)\n\s*\)\n", source, re.S)
        self.assertEqual(len(calls), 1)
        self.assertIn("interactive=True", calls[0])


class ProtectedFailClosedTests(ProtectedCase):
    """(3) a user.yaml that cannot be parsed keeps protecting the keys it names."""

    def test_unparseable_yaml_still_protects_its_key_file(self) -> None:
        key = self.root / "elsewhere" / "broken_key"
        write(self.cfg / "garbled" / "user.yaml", f"terminal_backends: [unclosed\n  x: {{type: ssh, key_file: {key.as_posix()}}}\n  - : :")
        self.assertIsNotNone(build_protected().reason(str(key)))

    def test_undecodable_yaml_still_protects_its_key_file(self) -> None:
        key = self.root / "elsewhere" / "binary_key"
        (self.cfg / "latin").mkdir()
        (self.cfg / "latin" / "user.yaml").write_bytes(b"\xff\xfe terminal_backends:\n  x: {type: ssh, key_file: " + key.as_posix().encode() + b"}\n")
        self.assertIsNotNone(build_protected().reason(str(key)))

    def test_parse_failure_is_logged(self) -> None:
        with mock.patch.object(protected_module.logger, "error") as error:
            build_protected()  # the fixture holds a plainly broken user.yaml
        self.assertTrue(any("cannot be parsed" in call.args[0] for call in error.call_args_list))


class WriteProtectionTests(ProtectedCase):
    """(4) the local backend may not write audit trails, configuration or known_hosts, nor touch MCP credentials."""

    def setUp(self) -> None:
        super().setUp()
        self.audit_log = write(self.root / "data" / "owner" / "agent1" / "terminal_audit.jsonl", "")
        self.agent_yaml = write(self.cfg / "owner" / "agents" / "agent1.yaml", "id: agent1\n")
        self.settings_yaml = write(self.cfg / "settings.yaml", "a: 1\n")
        self.mcp = write(self.cfg / "owner" / "mcp" / "srv" / "token.json", "{}")
        self.protected_writes = [self.audit_log, self.user_yaml, self.agent_yaml, self.settings_yaml, self.hosts]

    def test_file_tools_refuse_writes_and_patches(self) -> None:
        session = self.session("off")
        for path in self.protected_writes + [self.mcp]:
            self.assertIn("protected path", files.sandbox_write(session, str(path), "x"), path)
            self.assertIn("protected path", files.sandbox_patch(session, str(path), "a", "b"), path)
        self.assertEqual(session.backend.calls, [])

    def test_file_tools_still_read_configuration_but_not_mcp(self) -> None:
        session = self.session("off")
        for path in (self.user_yaml, self.agent_yaml, self.hosts):
            self.assertNotIn("protected path", files.sandbox_read(session, str(path)), path)
        self.assertIn("protected path", files.sandbox_read(session, str(self.mcp)))
        self.assertIn("protected path", files.sandbox_search(session, "x", str(self.mcp.parent)))

    def test_shell_writes_are_refused(self) -> None:
        session = self.session("off")
        for path in self.protected_writes:
            p = path.as_posix()
            for command in (
                f"echo x > {p}", f"echo x >> {p}", f"echo x | tee {p}", f"echo x | tee -a {p}", f"cp {self.work.as_posix()}/a {p}",
                f"mv {self.work.as_posix()}/a {p}", f"mv {p} {self.work.as_posix()}/gone", f"sed -i s/a/b/ {p}",
                f"perl -pi -e 's/a/b/' {p}", f"cp -t {path.parent.as_posix()} a {p}", f"dd if=/dev/null of={p}",
                f"truncate -s 0 {p}", f"rm {p}", f"install -m 600 a {p}", f"bash -c 'echo x > {p}'",
            ):
                self.assertDenied(session, command)

    def test_shell_reads_of_configuration_still_run(self) -> None:
        session = self.session("off")
        for path in (self.user_yaml, self.agent_yaml, self.settings_yaml, self.hosts, self.audit_log):
            p = path.as_posix()
            for command in (f"cat {p}", f"grep a {p}", f"cp {p} {self.work.as_posix()}/copy", f"sed -n 1p {p}", f"wc -l < {p}"):
                self.assertRuns(session, command)

    def test_mcp_credentials_are_unreadable_from_the_shell(self) -> None:
        session = self.session("off")
        self.assertDenied(session, f"cat {self.mcp.as_posix()}")
        self.assertDenied(session, f"cp {self.mcp.as_posix()} {self.work.as_posix()}/t")

    def test_ssh_backends_are_not_affected(self) -> None:
        session = self.session("off", local=False)
        _, denial = self.run_cmd(session, f"echo x > {self.user_yaml.as_posix()}")
        self.assertNotIn("protected path", denial or "")  # (only the ordinary unattended policy may speak)
        self.assertNotIn("protected path", files.sandbox_write(session, str(self.user_yaml), "x"))

    def test_protected_wins_over_off_and_unattended(self) -> None:
        for mode in ("manual", "smart", "off"):
            session = self.session(mode, unattended=True)
            self.assertDenied(session, f"echo x > {self.user_yaml.as_posix()}")


class EnvAllowlistTests(unittest.TestCase):
    """(5) local processes inherit an allowlist, never ambient credentials."""

    def test_only_the_allowlist_passes(self) -> None:
        env = {
            "PATH": "/usr/bin", "HOME": "/home/user-a", "LANG": "C", "LC_ALL": "C", "LC_CTYPE": "C", "TERM": "xterm",
            "TMPDIR": "/tmp", "USER": "user-a", "SHELL": "/bin/bash",
            "SSH_AUTH_SOCK": "/agent", "DOCKER_HOST": "tcp://h", "DBUS_SESSION_BUS_ADDRESS": "unix:x", "KUBECONFIG": "/k",
            "DATABASE_URL": "postgres://u:p@h/db", "HTTPS_PROXY": "http://p", "RECALL_SESSION_SECRET": "s",
            "OPENROUTER_API_KEY": "k", "EDITOR": "vi", "AWS_PROFILE": "x",
        }
        self.assertEqual(set(scrubbed_env(env)), {"PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "USER", "SHELL"})

    def test_local_backend_uses_it(self) -> None:
        from src.config.models import LocalBackendConfig
        from src.terminal.backends import LocalBackend

        backend = LocalBackend("here", LocalBackendConfig(type="local", workdir=tempfile.gettempdir()))
        with mock.patch.dict("os.environ", {"SSH_AUTH_SOCK": "/agent", "DATABASE_URL": "x", "PATH": "/usr/bin:/bin"}):
            result = backend.run("env", tempfile.gettempdir(), 10)
        names = {line.split("=", 1)[0] for line in result.stdout.splitlines()}
        self.assertNotIn("SSH_AUTH_SOCK", names)
        self.assertNotIn("DATABASE_URL", names)
        self.assertIn("PATH", names)


class SshArgvTests(unittest.TestCase):
    """(6) ssh never reads the server user's own ssh configuration."""

    def test_argv_ignores_user_config_and_keeps_strict_options(self) -> None:
        argv = SshBackend("box", ssh_config(), ssh_binary="ssh").argv("ls", "~")
        self.assertEqual(argv[:3], ["ssh", "-F", "/dev/null"])
        for option in ("IdentitiesOnly=yes", "StrictHostKeyChecking=yes", "BatchMode=yes"):
            self.assertIn(option, argv)


class AuditFailClosedTests(unittest.TestCase):
    """(7) no audit, no action; early refusals leave a record."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.audit = self.dir / "audit.jsonl"

    def records(self):
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def test_unwritable_audit_refuses_the_action(self) -> None:
        blocker = self.dir / "file"
        blocker.write_text("x")
        backend = FakeBackend()
        session = make_session(backend, "off", audit_file=blocker / "audit.jsonl")
        with run_context(RunContext("u", "a", "c", None, lambda *a: None, interactive=True)):
            result, denial = session.run("terminal", "ls", "ls")
        self.assertIsNone(result)
        self.assertEqual(denial, AUDIT_FAILED)
        self.assertEqual(backend.calls, [])

    def test_helpers_report_failure(self) -> None:
        blocker = self.dir / "file"
        blocker.write_text("x")
        self.assertFalse(audit_writable(blocker / "a.jsonl"))
        self.assertFalse(write_audit(blocker / "a.jsonl", tool="t"))
        self.assertTrue(write_audit(self.audit, tool="t"))
        self.assertTrue(audit_writable(None))

    def test_early_refusals_are_audited(self) -> None:
        session = make_session(FakeBackend(), "off", audit_file=self.audit)
        self.assertIn("NUL", session.run("terminal", "a\x00b", "a\x00b")[1])
        self.assertIn("longer than", session.run("terminal", "x", "x" * 20_001)[1])
        with self.assertRaises(ValueError):
            files.sandbox_write(session, "", "x")
        with self.assertRaises(ValueError):
            files.sandbox_read(session, "a\x00b")
        with self.assertRaises(ValueError):
            files.sandbox_search(session, "", ".")
        records = self.records()
        self.assertEqual(len(records), 5)
        self.assertEqual({r["decision"] for r in records}, {"denied:invalid"})
        self.assertEqual({r["tool"] for r in records}, {"terminal", "sandbox_write", "sandbox_read", "sandbox_search"})


class ApprovalPreviewTests(unittest.TestCase):
    """(8) the approval card of a file write shows what will be written."""

    def ask(self, call):
        broker = StubBroker(True)
        backend = FakeBackend()
        session = make_session(backend, "manual")
        responder = Responder(broker, None)  # type: ignore[arg-type]
        with run_context(responder.context()):
            call(session)
        return broker.requests

    def test_write_card_shows_a_truncated_preview(self) -> None:
        body = "first line\n" + "x" * 5000 + "TAILMARK"
        (request,) = self.ask(lambda s: files.sandbox_write(s, "a.txt", body))
        self.assertIn("write a.txt", request["command"])
        self.assertIn("first line", request["command"])
        self.assertNotIn("TAILMARK", request["command"])
        self.assertIn("more characters", request["command"])
        self.assertLess(len(request["command"]), files.PREVIEW_CHARS + 300)

    def test_patch_card_shows_old_and_new(self) -> None:
        (request, *_) = self.ask(lambda s: files.sandbox_patch(s, "a.txt", "OLD TEXT", "NEW TEXT"))
        self.assertIn("OLD TEXT", request["command"])
        self.assertIn("NEW TEXT", request["command"])

    def test_the_preview_is_not_audited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "a.jsonl"
            session = make_session(FakeBackend(), "manual", audit_file=audit)
            with run_context(Responder(StubBroker(True), None).context()):  # type: ignore[arg-type]
                files.sandbox_write(session, "a.txt", "SECRETBODY")
            self.assertNotIn("SECRETBODY", audit.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
