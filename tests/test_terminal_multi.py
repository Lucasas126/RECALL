"""One agent, several terminal backends: per-call choice, per-backend approvals, payloads, schema, notice."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from terminal_helpers import FakeBackend, Responder, resolved_multi
from src.agent.toolsets.terminal import build_terminal_tools, terminal_notice
from src.terminal.approvals import ApprovalBroker
from src.terminal.audit import audit_path
from src.terminal.context import run_context

TOOLS = ("terminal", "sandbox_read", "sandbox_write", "sandbox_patch", "sandbox_search")


class MultiBackendFixture(unittest.TestCase):
    def build(self, *, unattended=False, **policies):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        patcher = patch("src.agent.toolsets.terminal.DATA_DIR", self.data_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        policies = policies or {"sandbox": ("smart", "~/workspace"), "pc": ("manual", "~")}
        self.backends = {name: FakeBackend() for name in policies}
        self.config = resolved_multi(**policies)
        tools = build_terminal_tools(self.config, unattended=unattended, backends=self.backends)
        self.tools = {tool.__name__: tool for tool in tools}
        return self.tools

    def run_attended(self, command, answer="approve", **kwargs):
        broker = ApprovalBroker(5)
        self.addCleanup(broker.close)
        responder = Responder(broker, answer)
        with run_context(responder.context()):
            text = self.tools["terminal"](command, **kwargs)
        return text, responder


class SelectionTests(MultiBackendFixture):
    def test_default_backend_is_used_without_a_choice(self) -> None:
        self.build()
        self.tools["terminal"]("ls")
        self.assertEqual((len(self.backends["sandbox"].calls), len(self.backends["pc"].calls)), (1, 0))
        self.assertEqual(self.backends["sandbox"].calls[0].cwd, "~/workspace")

    def test_the_model_can_choose_another_backend_and_its_workdir_applies(self) -> None:
        self.build(sandbox=("off", "~/workspace"), pc=("off", "~/projects"))
        self.tools["terminal"]("ls", backend="pc")
        self.assertEqual(len(self.backends["sandbox"].calls), 0)
        self.assertEqual(self.backends["pc"].calls[0].cwd, "~/projects")

    def test_default_can_be_the_second_backend(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch("src.agent.toolsets.terminal.DATA_DIR", Path(directory.name)):
            backends = {"a": FakeBackend(), "b": FakeBackend()}
            tools = build_terminal_tools(resolved_multi(default="b", a=("off", "~"), b=("off", "~")), backends=backends)
            tools[0]("ls")
        self.assertEqual((len(backends["a"].calls), len(backends["b"].calls)), (0, 1))

    def test_unknown_backend_is_a_plain_text_error_listing_the_allowed_names(self) -> None:
        self.build()
        for name in ("nas", "PC", "../pc"):
            for tool, args in (("terminal", ("ls",)), ("sandbox_read", ("a",)), ("sandbox_write", ("a", "b")),
                               ("sandbox_patch", ("a", "b", "c")), ("sandbox_search", ("x",))):
                text = self.tools[tool](*args, backend=name)
                self.assertIn("unknown backend", text)
                self.assertIn("sandbox, pc", text)
        self.assertEqual([len(b.calls) for b in self.backends.values()], [0, 0])

    def test_every_tool_accepts_the_backend_parameter(self) -> None:
        self.build(sandbox=("off", "~"), pc=("off", "~"))
        self.tools["sandbox_read"]("a.txt", backend="pc")
        self.tools["sandbox_search"]("x", backend="pc")
        self.assertEqual((len(self.backends["sandbox"].calls), len(self.backends["pc"].calls)), (0, 2))


class SchemaTests(MultiBackendFixture):
    def test_schema_enumerates_the_allowed_backends(self) -> None:
        self.build()
        for name in TOOLS:
            spec = self.tools[name].tool_spec["function"]["parameters"]
            self.assertEqual(spec["properties"]["backend"]["enum"], ["sandbox", "pc"], name)
            self.assertNotIn("backend", spec["required"])
            self.assertIn("Default: sandbox", spec["properties"]["backend"]["description"])

    def test_a_single_backend_needs_no_parameter(self) -> None:
        self.build(box=("off", "~"))
        for name in TOOLS:
            self.assertNotIn("backend", self.tools[name].tool_spec["function"]["parameters"]["properties"])
        self.tools["terminal"]("ls", backend="box")  # still tolerated if a model sends it
        self.assertEqual(len(self.backends["box"].calls), 1)
        self.assertIn("unknown backend", self.tools["terminal"]("ls", backend="pc"))

    def test_a_backend_that_did_not_resolve_is_not_offered(self) -> None:
        config = resolved_multi(sandbox=("smart", "~"), pc=("manual", "~"))
        config = config.model_copy(update={"terminal_backends": {"sandbox": config.terminal_backends["sandbox"]}})
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch("src.agent.toolsets.terminal.DATA_DIR", Path(directory.name)):
            tools = {t.__name__: t for t in build_terminal_tools(config, backends={"sandbox": FakeBackend()})}
        self.assertNotIn("backend", tools["terminal"].tool_spec["function"]["parameters"]["properties"])
        self.assertIn("unknown backend 'pc'", tools["terminal"]("ls", backend="pc"))
        self.assertNotIn("`pc`", terminal_notice(config))


class ApprovalPerBackendTests(MultiBackendFixture):
    def test_same_command_runs_in_smart_sandbox_but_asks_on_manual_pc(self) -> None:
        self.build()
        text, responder = self.run_attended("ls", answer="approve")
        self.assertEqual((responder.kinds(), len(self.backends["sandbox"].calls)), ([], 1))
        text, responder = self.run_attended("ls", answer="approve", backend="pc")
        self.assertEqual(responder.kinds(), ["approval", "approval_resolved"])
        self.assertEqual(len(self.backends["pc"].calls), 1)

    def test_denial_on_the_pc_never_runs_there(self) -> None:
        self.build()
        text, responder = self.run_attended("ls", answer="deny", backend="pc")
        self.assertIn("Denied", text)
        self.assertEqual(len(self.backends["pc"].calls), 0)

    def test_payload_and_decision_log_carry_the_backend(self) -> None:
        self.build()
        broker = ApprovalBroker(5)
        self.addCleanup(broker.close)
        responder = Responder(broker, "approve")
        with run_context(responder.context()):
            self.tools["terminal"]("ls", backend="pc")
            self.tools["terminal"]("rm -rf build")  # dangerous in smart mode: asks, on the sandbox
        payloads = [data for kind, data in responder.events if kind == "approval"]
        self.assertEqual([p["backend"] for p in payloads], ["pc", "sandbox"])
        self.assertEqual([d["backend"] for d in broker.recent_decisions("u")], ["pc", "sandbox"])

    def test_hardline_is_denied_on_every_backend(self) -> None:
        self.build(sandbox=("off", "~"), pc=("off", "~"))
        for name in ("sandbox", "pc"):
            self.assertIn("hardline", self.tools["terminal"]("rm -rf " + "/", backend=name))
        self.assertEqual([len(b.calls) for b in self.backends.values()], [0, 0])

    def test_unattended_policy_uses_each_backends_mode(self) -> None:
        self.build(unattended=True)
        self.assertIn("exit code: 0", self.tools["terminal"]("ls"))  # smart: normal command runs
        self.assertIn("nobody is attached", self.tools["terminal"]("ls", backend="pc"))  # manual: denied
        self.assertEqual((len(self.backends["sandbox"].calls), len(self.backends["pc"].calls)), (1, 0))


class AuditAndLogTests(MultiBackendFixture):
    def test_audit_lines_carry_the_configured_backend_name(self) -> None:
        self.build(sandbox=("off", "~"), pc=("manual", "~"))
        self.tools["terminal"]("ls")
        self.tools["terminal"]("ls", backend="pc")  # unattended manual: denied, still audited
        lines = [json.loads(l) for l in audit_path(self.data_dir, "u", "a").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([(l["backend"], l["decision"]) for l in lines],
                         [("sandbox", "auto:off"), ("pc", "denied:unattended")])

    def test_log_line_names_the_backend(self) -> None:
        self.build(sandbox=("off", "~"), pc=("off", "~"))
        with self.assertLogs("recall.terminal", "INFO") as logs:
            self.tools["terminal"]("ls", backend="pc")
        self.assertTrue(any("[pc]" in line for line in logs.output))


class NoticeTests(unittest.TestCase):
    def test_lists_each_backend_with_workdir_description_mode_and_default(self) -> None:
        config = resolved_multi(
            sandbox=("smart", "~/workspace"), pc=("manual", "~"),
            descriptions={"pc": "User A's Windows PC, real files, Git Bash"},
        )
        notice = terminal_notice(config)
        self.assertIn("`sandbox` (default)", notice)
        self.assertNotIn("`pc` (default)", notice)
        self.assertIn("User A's Windows PC, real files, Git Bash", notice)
        self.assertIn("Working directory ~/workspace", notice)
        self.assertIn("only dangerous actions ask first", notice)
        self.assertIn("every command and file operation asks first", notice)
        self.assertIn("real machines", notice)


if __name__ == "__main__":
    unittest.main()
