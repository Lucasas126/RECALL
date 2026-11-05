from __future__ import annotations

import io
import json
import contextlib
import os
import shlex
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from terminal_helpers import FakeBackend, FakeSandbox, Responder, StubBroker, make_session, resolved_agent, resolved_multi, ssh_config
from src.admin import users as admin
from src.agent import dream
from src.agent.toolsets.terminal import build_terminal_tools, terminal_notice
from src.config.models import (
    AgentConfig, AgentTerminalBackend, AgentTerminalConfig, DreamConfig, LocalBackendConfig, ProviderConfig, SshBackendConfig,
    TerminalSettings, UserConfig,
)
from src.core.sources.registry import SourceRegistry
from src.terminal import backends as backends_module
from src.terminal.approvals import ApprovalBroker
from src.terminal.audit import audit_path
from src.terminal.backends import LocalBackend, Result, SshBackend, build_backend, minimal_env, scrubbed_env
from src.terminal.context import run_context
from src.terminal.output import REDACTED, clean, redact, strip_ansi, truncate
from src.terminal.resolve import check_agent_terminal, resolve_backend, resolve_backends
from src.utils.i18n import InputError

HOSTILE = "'; rm -rf / #"


class ToolFixture(unittest.TestCase):
    def build(self, backend, approvals="off", **terminal):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        patcher = patch("src.agent.toolsets.terminal.DATA_DIR", self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        tools = build_terminal_tools(resolved_agent(approvals, **terminal), backends={"box": backend})
        self.tools = {tool.__name__: tool for tool in tools}
        return self.tools


class TerminalToolTests(ToolFixture):
    def test_five_tools_with_specs(self) -> None:
        tools = self.build(FakeBackend())
        self.assertEqual(set(tools), {"terminal", "sandbox_read", "sandbox_write", "sandbox_patch", "sandbox_search"})
        self.assertEqual(tools["terminal"].tool_spec["function"]["parameters"]["required"], ["command"])

    def test_no_backend_no_tools(self) -> None:
        config = resolved_agent().model_copy(update={"terminal_backends": {}})
        self.assertEqual(build_terminal_tools(config), [])
        self.assertIsNone(terminal_notice(config))
        self.assertIn("`box` (default)", terminal_notice(resolved_agent("smart")))

    def test_result_format_and_cwd(self) -> None:
        backend = FakeBackend(lambda c, cwd, t, s: Result(2, "out\n", "boom\n"))
        text = self.build(backend)["terminal"]("make", timeout=7, workdir="~/proj")
        self.assertEqual(text, "exit code: 2\nstdout:\nout\n\nstderr:\nboom\n")
        call = backend.calls[0]
        self.assertEqual((call.command, call.cwd, call.timeout), ("make", "~/proj", 7.0))

    def test_timeout_and_no_output(self) -> None:
        backend = FakeBackend(lambda *a: Result(None, timed_out=True))
        self.assertIn("timed out after 60s", self.build(backend)["terminal"]("sleep 999"))
        backend = FakeBackend(lambda *a: Result(0))
        self.assertEqual(self.build(backend)["terminal"]("true"), "exit code: 0\n(no output)")

    def test_output_is_redacted_and_truncated(self) -> None:
        secret = "sk-or-" + "a1b2c3d4" * 4
        backend = FakeBackend(lambda *a: Result(0, f"key {secret}\n" + "x" * 5000))
        text = self.build(backend, max_output_chars=500)["terminal"]("env")
        self.assertNotIn(secret, text)
        self.assertIn(REDACTED, text)
        self.assertIn("characters omitted", text)

    def test_hardline_never_reaches_the_backend(self) -> None:
        backend = FakeBackend()
        text = self.build(backend, "off")["terminal"]("rm -rf " + "/")
        self.assertIn("hardline", text)
        self.assertEqual(backend.calls, [])


class FileToolTests(ToolFixture):
    def sandbox(self, files=None, **kw):
        self.box = FakeSandbox(files)
        return self.build(self.box, **kw)

    def test_read_paginates(self) -> None:
        tools = self.sandbox({"/w/big.txt": "\n".join(f"line {i}" for i in range(1, 451))})
        first = tools["sandbox_read"]("/w/big.txt")
        self.assertIn("     1|line 1", first)
        self.assertIn("   200|line 200", first)
        self.assertNotIn("line 201", first)
        self.assertIn("lines 1-200 of 450] Use offset=201 to continue.", first)
        last = tools["sandbox_read"]("/w/big.txt", offset=401, limit=100)
        self.assertIn("lines 401-450 of 450]", last)
        self.assertNotIn("continue", last)
        self.assertIn("past the end", tools["sandbox_read"]("/w/big.txt", offset=900))
        self.assertIn("no such file", tools["sandbox_read"]("/w/missing"))

    def test_write_sends_content_on_stdin_only(self) -> None:
        tools = self.sandbox()
        content = "secret body; rm -rf x\n$(touch y)\n"
        self.assertEqual(tools["sandbox_write"]("notes/a.txt", content), f"wrote {len(content)} bytes to notes/a.txt")
        call = self.box.calls[0]
        self.assertEqual(call.stdin, content.encode())
        self.assertNotIn("secret body", call.command)
        self.assertNotIn("touch y", call.command)
        self.assertEqual(self.box.files["notes/a.txt"], content)

    def test_write_into_a_system_path_asks_even_in_smart_mode_and_a_device_is_hardline(self) -> None:
        tools = self.sandbox(approvals="smart")
        self.assertIn("hardline", tools["sandbox_write"]("/dev/sda", "x"))
        self.assertEqual(self.box.calls, [])
        # unattended smart mode: dangerous (system path) is denied, a workspace file is fine
        self.assertIn("Denied", tools["sandbox_write"]("/etc/hosts", "x"))
        self.assertEqual(self.box.calls, [])
        self.assertIn("wrote", tools["sandbox_write"]("/srv/w/ok.txt", "x"))

    def test_patch_unique_with_diff(self) -> None:
        tools = self.sandbox({"a.py": "x = 1\ny = 2\nz = 3\n"})
        result = tools["sandbox_patch"]("a.py", "y = 2", "y = 20")
        self.assertIn("patched a.py (exact)", result)
        self.assertIn("-y = 2", result)
        self.assertIn("+y = 20", result)
        self.assertEqual(self.box.files["a.py"], "x = 1\ny = 20\nz = 3\n")
        self.assertNotIn("y = 20", self.box.calls[-1].command)  # content travels on stdin
        self.assertIn(b"y = 20", self.box.calls[-1].stdin)

    def test_patch_duplicate_missing_and_replace_all(self) -> None:
        tools = self.sandbox({"a.txt": "foo\nbar\nfoo\n"})
        self.assertIn("matches 2 places", tools["sandbox_patch"]("a.txt", "foo", "baz"))
        self.assertEqual(self.box.files["a.txt"], "foo\nbar\nfoo\n")
        self.assertIn("was not found", tools["sandbox_patch"]("a.txt", "nothing", "x"))
        self.assertIn("2 replacements", tools["sandbox_patch"]("a.txt", "foo", "baz", replace_all=True))
        self.assertEqual(self.box.files["a.txt"], "baz\nbar\nbaz\n")
        self.assertIn("no such file", tools["sandbox_patch"]("nope.txt", "a", "b"))
        self.assertIn("identical", tools["sandbox_patch"]("a.txt", "bar", "bar"))

    def test_patch_tolerates_indentation_differences(self) -> None:
        tools = self.sandbox({"a.py": "def f():\n    return 1\n"})
        self.assertIn("whitespace-tolerant", tools["sandbox_patch"]("a.py", "def f():\nreturn 1", "def f():\n    return 2"))
        self.assertIn("return 2", self.box.files["a.py"])

    def test_one_approval_covers_a_whole_patch(self) -> None:
        broker = ApprovalBroker(5)
        self.addCleanup(broker.close)
        tools = self.sandbox({"a.txt": "one\n"}, approvals="manual")
        responder = Responder(broker, "approve")
        with run_context(responder.context()):
            tools["sandbox_patch"]("a.txt", "one", "two")
        self.assertEqual(responder.kinds().count("approval"), 1)
        self.assertEqual(self.box.files["a.txt"], "two\n")

    def test_search_contents_and_names(self) -> None:
        tools = self.sandbox({"/w/a.py": "import os\nprint(1)\n", "/w/b.txt": "import me\n", "/w/c.py": "pass\n"})
        found = tools["sandbox_search"]("^import", "/w")
        self.assertIn("/w/a.py:1:import os", found)
        self.assertIn("/w/b.txt:1:import me", found)
        self.assertEqual(tools["sandbox_search"]("^import", "/w", glob="*.py"), "/w/a.py:1:import os")
        self.assertEqual(tools["sandbox_search"]("zzz", "/w"), "no matches")
        self.assertEqual(set(tools["sandbox_search"]("*.py", "/w", files=True).splitlines()), {"/w/a.py", "/w/c.py"})

    def test_content_and_patterns_never_land_in_argv_unquoted(self) -> None:
        tools = self.sandbox({"a.txt": "x\n"})
        tools["sandbox_search"](HOSTILE, "a.txt")
        script = self.box.calls[-1].command
        self.assertIn(shlex.quote(HOSTILE), script)  # one quoted word, not shell syntax
        tools["sandbox_read"]("a b; touch c.txt")
        self.assertTrue(self.box.calls[-1].command.startswith("f='a b; touch c.txt'"))


class OutputTests(unittest.TestCase):
    def test_ansi_and_control_characters_are_removed(self) -> None:
        self.assertEqual(strip_ansi("\x1b[31mred\x1b[0m\r\nok\x07\x00"), "red\nok")

    def test_redaction(self) -> None:
        key = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"
        samples = ["sk-or-" + "x" * 20, "sk-ant-" + "y" * 20, "ghp_" + "z" * 30, "tvly-" + "w" * 16,
                   "Authorization: Bearer abcdef123456", "123456789:" + "A" * 35, key]
        for sample in samples:
            with self.subTest(sample=sample[:12]):
                self.assertNotIn(sample.split("Bearer ")[-1], redact(f"before {sample} after"))
        self.assertEqual(redact("plain text"), "plain text")

    def test_truncate_keeps_head_and_tail(self) -> None:
        text, cut = truncate("a" * 100 + "b" * 100, 50)
        self.assertTrue(cut)
        self.assertTrue(text.startswith("a" * 20))
        self.assertTrue(text.endswith("b" * 30))
        self.assertIn("150 characters omitted", text)
        self.assertEqual(truncate("short", 50), ("short", False))

    def test_secret_cannot_hide_across_the_cut(self) -> None:
        secret = "sk-or-" + "k" * 40
        text, _ = clean("p" * 4000 + secret + "q" * 4000, 600)
        self.assertNotIn("sk-or-k", text)


class SshBackendTests(unittest.TestCase):
    def fake_popen(self, returncode=0):
        captured = {}

        class FakeProc:
            pid = 1

            def __init__(self, argv, **kwargs):
                captured["argv"], captured["kwargs"] = argv, kwargs
                self.returncode = returncode

            def communicate(self, data=None, timeout=None):
                captured["stdin"] = data
                return b"out", b""

        return captured, patch.object(backends_module.subprocess, "Popen", FakeProc)

    def test_argv_is_hardened_and_the_hostile_command_is_one_remote_argument(self) -> None:
        backend = SshBackend("box", ssh_config(port=2222), ssh_binary="ssh")
        argv = backend.argv(HOSTILE, "~/work space")
        self.assertEqual(argv[-2], "--")
        self.assertEqual(argv[-3], "agent@192.0.2.10")
        for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "IdentitiesOnly=yes", "ForwardAgent=no",
                       "ForwardX11=no", "UserKnownHostsFile=" + str(Path("/keys/known_hosts"))):
            self.assertIn(option, argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        remote = shlex.split(argv[-1])  # what the remote login shell parses
        self.assertEqual(remote[:2], ["bash", "-lc"])
        self.assertEqual(len(remote), 3)
        self.assertTrue(remote[2].endswith(" && " + HOSTILE))
        self.assertTrue(remote[2].startswith('cd "$HOME"/'))
        self.assertEqual(sum(1 for part in argv if "rm -rf" in part), 1)  # only inside the one remote argument

    def test_run_never_uses_a_local_shell_and_passes_a_minimal_env(self) -> None:
        captured, popen = self.fake_popen()
        env = {"PATH": "/usr/bin", "HOME": "/home/x", "OPENROUTER_API_KEY": "s3cret", "SSH_AUTH_SOCK": "/agent",
               "RECALL_SESSION_SECRET": "s", "HTTPS_PROXY": "http://p"}
        with popen, patch.dict(os.environ, env, clear=True):
            result = SshBackend("box", ssh_config(), ssh_binary="ssh").run("ls", "~", 5, stdin="data")
        self.assertEqual((result.exit_code, result.stdout), (0, "out"))
        self.assertIsInstance(captured["argv"], list)
        self.assertFalse(captured["kwargs"].get("shell", False))
        self.assertEqual(set(captured["kwargs"]["env"]), {"PATH", "HOME"})
        self.assertEqual(captured["stdin"], b"data")

    def test_exit_255_explains_that_ssh_failed(self) -> None:
        captured, popen = self.fake_popen(255)
        with popen:
            result = SshBackend("box", ssh_config(), ssh_binary="ssh").run("ls", "~", 5)
        self.assertIn("could not reach or authenticate", result.stderr)

    def test_env_helpers(self) -> None:
        env = {"PATH": "p", "MY_API_KEY": "k", "GITHUB_TOKEN": "t", "LANG": "C", "RECALL_X": "1", "EDITOR": "vi"}
        self.assertEqual(set(scrubbed_env(env)), {"PATH", "LANG", "EDITOR"})
        self.assertEqual(set(minimal_env(env)), {"PATH", "LANG"})

    def test_build_backend_picks_the_class(self) -> None:
        self.assertIsInstance(build_backend("a", ssh_config()), SshBackend)
        self.assertIsInstance(build_backend("b", LocalBackendConfig(type="local", workdir="/tmp")), LocalBackend)


class ResolveAndConfigTests(unittest.TestCase):
    def user(self, **backends):
        return UserConfig(id="u", name="U", provider=ProviderConfig(name="ollama"), agents=[], terminal_backends=backends)

    def test_local_backend_is_refused_unless_allowed(self) -> None:
        user = self.user(here={"type": "local", "workdir": "/srv/w"}, box=ssh_config().model_dump())
        with self.assertRaises(InputError):
            resolve_backend(user, "here", allow_local=False)
        self.assertEqual(resolve_backend(user, "here", allow_local=True).type, "local")
        self.assertEqual(resolve_backend(user, "box", allow_local=False).type, "ssh")

    def test_missing_block_or_backend(self) -> None:
        user = self.user(box=ssh_config().model_dump())
        both = AgentTerminalConfig(backends={"box": {}, "other": {}}, default_backend="box")
        with self.assertRaises(InputError):
            resolve_backends(user, None, agent_id="a", allow_local=False)
        with self.assertRaisesRegex(InputError, "not defined"):
            resolve_backend(user, "other", allow_local=False)
        with self.assertRaises(InputError):  # none resolves
            resolve_backends(user, AgentTerminalConfig(backends={"other": {}}), agent_id="a", allow_local=False)
        self.assertEqual(list(resolve_backends(user, both, agent_id="a", allow_local=False)), ["box"])
        check_agent_terminal(user, ["memory_read"], None, agent_id="a", allow_local=False)  # group absent: nothing to check
        with self.assertRaises(InputError):
            check_agent_terminal(user, ["terminal"], None, agent_id="a", allow_local=False)

    def test_one_backend_failing_keeps_the_others_and_logs_a_warning(self) -> None:
        user = self.user(here={"type": "local", "workdir": "/srv/w"}, box=ssh_config().model_dump())
        term = AgentTerminalConfig(backends={"here": {}, "box": {}, "gone": {}}, default_backend="box")
        with self.assertLogs("recall.terminal", "WARNING") as logs:
            resolved = resolve_backends(user, term, agent_id="a", allow_local=False)
        self.assertEqual(list(resolved), ["box"])
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(list(resolve_backends(user, term, agent_id="a", allow_local=True)), ["here", "box"])

    def test_ssh_config_validation(self) -> None:
        good = ssh_config().model_dump()
        for change in ({"host": "bad host; ls"}, {"host": "-oProxyCommand=x"}, {"port": 0}, {"port": "22"},
                       {"user": "a b"}, {"key_file": ""}, {"password": "pw"}, {"type": "telnet"}):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                SshBackendConfig.model_validate({**good, **change})

    def test_agent_terminal_validation_and_defaults(self) -> None:
        term = AgentTerminalConfig(backends={"box": {}})
        entry = term.backends["box"]
        self.assertEqual((entry.approvals, entry.workdir, entry.timeout_seconds), ("manual", "~", None))
        self.assertEqual((term.default_backend, term.timeout_seconds, term.allow_in_dream), ("box", 60, False))
        two = {"sandbox": {"approvals": "smart", "timeout_seconds": 5}, "pc": {}}
        self.assertEqual(AgentTerminalConfig(backends=two, default_backend="pc").timeout_for_backend("sandbox"), 5)
        self.assertEqual(AgentTerminalConfig(backends=two, default_backend="pc").timeout_for_backend("pc"), 60)
        for bad in ({"backends": {}}, {"backends": {"Bad Name": {}}}, {"backends": {"box": {"approvals": "always"}}},
                    {"backends": {"box": {"timeout_seconds": 0}}}, {"backends": {"box": {"extra": 1}}},
                    {"backends": two},  # several backends need a default
                    {"backends": two, "default_backend": "nas"},  # default must be allowed
                    {"backends": {"box": {}}, "default_backend": "pc"},
                    {"backend": "box"},  # the old shape is gone
                    {"timeout_seconds": 0}, {"timeout_seconds": 601}, {"max_output_chars": 10}, {"extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                AgentTerminalConfig.model_validate({"backends": {"box": {}}, **bad})

    def test_backend_description_is_limited_to_200_characters(self) -> None:
        self.assertEqual(ssh_config(description="x" * 200).description, "x" * 200)
        with self.assertRaises(ValidationError):
            ssh_config(description="x" * 201)
        with self.assertRaises(ValidationError):
            LocalBackendConfig(type="local", workdir="/w", description="x" * 201)
        self.assertEqual(ssh_config().description, "")

    def test_user_backend_names_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            self.user(**{"Bad Name": ssh_config().model_dump()})

    def test_settings_validation(self) -> None:
        self.assertFalse(TerminalSettings().allow_local)
        with self.assertRaises(ValidationError):
            TerminalSettings(default_timeout_seconds=100, max_timeout_seconds=50)
        with self.assertRaises(ValidationError):
            TerminalSettings(approval_timeout_seconds=1)

    def test_dream_terminal_needs_the_agent_opt_in(self) -> None:
        with self.assertRaises(ValidationError):
            AgentConfig(id="a", name="A", dream=DreamConfig(tools=["terminal"]), terminal={"backends": {"box": {}}})
        agent = AgentConfig(id="a", name="A", dream=DreamConfig(tools=["terminal"]),
                            terminal={"backends": {"box": {}}, "allow_in_dream": True})
        self.assertTrue(agent.terminal.allow_in_dream)


class DreamGatingTests(unittest.TestCase):
    def tools(self, *, dream_tools, allow_in_dream, backend=True):
        config = resolved_agent(allow_in_dream=allow_in_dream).model_copy(update={"dream": DreamConfig(tools=dream_tools)})
        if not backend:
            config = config.model_copy(update={"terminal_backends": {}})
        host = SimpleNamespace(config=config, sources=SimpleNamespace(folders=lambda: []))
        with contextlib.ExitStack() as stack:
            for name in ("build_review_tools", "build_memory_tools", "build_tag_tools", "build_edit_tools",
                         "build_self_tools"):
                stack.enter_context(patch.object(dream, name, return_value=[]))
            specs, handlers = dream.build_dream_tools(host, None, {}, [], None)
        return set(handlers)

    def test_needs_both_allow_in_dream_and_the_dream_tools_entry(self) -> None:
        names = {"terminal", "sandbox_read", "sandbox_write", "sandbox_patch", "sandbox_search"}
        self.assertEqual(self.tools(dream_tools=["terminal"], allow_in_dream=True), names)
        self.assertEqual(self.tools(dream_tools=["web"], allow_in_dream=True), set())
        self.assertEqual(self.tools(dream_tools=["terminal"], allow_in_dream=False), set())  # defence in depth
        self.assertEqual(self.tools(dream_tools=["terminal"], allow_in_dream=True, backend=False), set())

    def test_dream_tools_never_prompt(self) -> None:
        backend = FakeBackend()
        config = resolved_agent("manual", allow_in_dream=True)
        with patch("src.agent.toolsets.terminal.DATA_DIR", Path("unused")), \
                patch("src.terminal.audit.write_audit"), patch("src.terminal.session.write_audit"):
            tools = {t.__name__: t for t in build_terminal_tools(config, unattended=True, backends={"box": backend})}
            broker = ApprovalBroker(1)
            responder = Responder(broker, "approve")
            with run_context(responder.context()):  # even inside an interactive run
                text = tools["terminal"]("ls")
        self.assertIn("nobody is attached", text)
        self.assertEqual((backend.calls, responder.events), ([], []))


class AuditTests(ToolFixture):
    def test_one_json_line_per_action_without_output_and_with_redaction(self) -> None:
        secret = "sk-or-" + "q" * 24
        backend = FakeBackend(lambda *a: Result(0, "STDOUT-MARKER"))
        tools = self.build(backend, "off")
        tools["terminal"](f"curl -H 'x: {secret}' http://192.0.2.5/")
        tools["terminal"]("rm -rf " + "/")
        path = audit_path(self.data_dir, "u", "a")
        self.assertEqual(path, self.data_dir / "u" / "a" / "terminal_audit.jsonl")
        raw = path.read_text(encoding="utf-8")
        lines = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertNotIn("STDOUT-MARKER", raw)
        self.assertNotIn(secret, raw)
        self.assertEqual((lines[0]["decision"], lines[0]["exit_code"], lines[0]["tool"]), ("auto:off", 0, "terminal"))
        self.assertEqual((lines[1]["decision"], lines[1]["exit_code"]), ("hardline", None))
        for key in ("ts", "user", "agent", "backend", "command", "cwd", "duration_ms"):
            self.assertIn(key, lines[0])

    def test_denied_actions_are_audited_too(self) -> None:
        tools = self.build(FakeBackend(), "manual")
        tools["terminal"]("ls")  # unattended manual: denied
        line = json.loads(audit_path(self.data_dir, "u", "a").read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(line["decision"], "denied:unattended")

    def test_the_audit_file_is_outside_every_source_the_agent_can_read(self) -> None:
        tools = self.build(FakeBackend(), "off")
        tools["terminal"]("ls")
        path = audit_path(self.data_dir, "u", "a")
        self.assertTrue(path.is_file())
        registry = SourceRegistry.for_agent("u", "a", data_dir=self.data_dir)
        for name in registry.names():  # what the memory/palace/folder tools can reach
            with self.subTest(source=name), self.assertRaises(Exception):
                registry.get(name).read("terminal_audit.jsonl")
        self.assertNotIn("terminal_audit.jsonl", registry.list("agent"))


class AdminTerminalTestTests(unittest.TestCase):
    def run_cli(self, argv, backends=None, user="ana", allow_local=False):
        loaded = UserConfig(id=user, name="A", provider=ProviderConfig(name="ollama"), agents=[],
                            terminal_backends=backends or {})
        out, err = io.StringIO(), io.StringIO()
        with patch.object(admin, "load_user", return_value=loaded), \
                patch.object(admin.TERMINAL, "allow_local", allow_local), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = admin.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_runs_the_probe_through_the_backend_and_prints_cleaned_output(self) -> None:
        secret = "sk-or-" + "z" * 24
        backend = FakeBackend(lambda *a: Result(0, f"ok\nLinux 6.1\n{secret}\n"))
        with patch.object(admin, "build_backend", return_value=backend) as build:
            code, out, err = self.run_cli(["terminal-test", "--user", "ana", "--backend", "box"],
                                          {"box": ssh_config().model_dump()})
        self.assertEqual(code, 0)
        self.assertEqual(build.call_args.args[0], "box")
        call = backend.calls[0]
        self.assertEqual((call.command, call.timeout), ("echo ok; uname -sr", 15))
        self.assertIn("exit code: 0", out)
        self.assertIn("Linux 6.1", out)
        self.assertNotIn(secret, out)
        self.assertNotIn("/keys/id_test", out + err)  # key file location/contents are never printed

    def test_failure_exit_code_and_stderr(self) -> None:
        backend = FakeBackend(lambda *a: Result(255, "", "Host key verification failed."))
        with patch.object(admin, "build_backend", return_value=backend):
            code, out, _ = self.run_cli(["terminal-test", "--user", "ana", "--backend", "box"], {"box": ssh_config().model_dump()})
        self.assertEqual(code, 1)
        self.assertIn("exit code: 255", out)
        self.assertIn("Host key verification failed.", out)

    def test_unknown_backend_and_disabled_local(self) -> None:
        code, _, err = self.run_cli(["terminal-test", "--user", "ana", "--backend", "nope"], {"box": ssh_config().model_dump()})
        self.assertEqual(code, 1)
        self.assertIn("box", err)
        backend = FakeBackend()
        with patch.object(admin, "build_backend", return_value=backend):
            code, _, err = self.run_cli(["terminal-test", "--user", "ana", "--backend", "here"],
                                        {"here": {"type": "local", "workdir": "/srv/w"}})
        self.assertEqual((code, backend.calls), (1, []))
        self.assertIn("allow_local", err)


if __name__ == "__main__":
    unittest.main()
