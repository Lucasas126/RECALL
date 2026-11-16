"""Approval rules at run time: the session and the file tools decide through the rules; audit, floor, unattended, loading."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from tests.support.admin_helpers import AdminFixture
from tests.support.terminal_helpers import FakeBackend, FakeSandbox, Responder, StubBroker, resolved_agent, ssh_config

from src.agent.toolsets.terminal import build_terminal_tools, terminal_notice
from src.config.levels import ApprovalRuleNotAllowed, Level
from src.config.loader import load_agent
from src.config.models import AgentTerminalBackend, AgentTerminalConfig, SshBackendConfig, TerminalSettings
from src.terminal import files
from src.terminal.approvals import ApprovalBroker
from src.terminal.context import RunContext, run_context
from src.terminal.rules import ApprovalRule, ApprovalRuleList, RuleSet
from src.terminal.session import AUDIT_FAILED, TerminalSession


def ruleset(installation=None, user=None, agent=None) -> RuleSet:
    def lst(value):
        return None if value is None else ApprovalRuleList.model_validate(value if isinstance(value, dict) else {"rules": value})

    return RuleSet.build(lst(installation), lst(user), lst(agent))


class SessionCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.audit = Path(directory.name) / "audit.jsonl"

    def session(self, rules=None, approvals: str | None = None, *, kind: str | None = None, backend=None, unattended=False,
                name: str = "box") -> TerminalSession:
        entry = AgentTerminalBackend(workdir="~/workspace", **({"approvals": approvals} if approvals else {}))
        return TerminalSession(
            backend or FakeBackend(), AgentTerminalConfig(backends={name: entry}), TerminalSettings(),
            user_id="u", agent_id="a", name=name, audit_file=self.audit, unattended=unattended, rules=rules, kind=kind,
        )

    def records(self) -> list[dict]:
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]

    def attended(self, answer="approve") -> Responder:
        return Responder(ApprovalBroker(2), answer=answer)

    def run_in(self, session, command, responder=None, interactive=True):
        responder = responder or self.attended()
        with run_context(responder.context(interactive=interactive)):
            return session.run("terminal", command, command), responder


class RuleDecisionTests(SessionCase):
    def test_an_allow_rule_runs_without_asking_and_says_so(self) -> None:
        backend = FakeBackend()
        session = self.session(ruleset(user=[{"verdict": "allow", "tool": "terminal", "command": "ls"}]), backend=backend)
        (result, denial), responder = self.run_in(session, "ls -l")
        self.assertIsNone(denial)
        self.assertEqual(responder.events, [])
        self.assertEqual(result.approval, "[ran without approval: allowed by user:0]")
        line = self.records()[0]
        self.assertEqual((line["decision"], line["layer"], line["rule"], line["rule_verdict"]), ("auto:rule", "user", "user:0", "allow"))
        self.assertNotIn("approval_id", line)

    def test_a_deny_rule_refuses_without_asking(self) -> None:
        backend = FakeBackend()
        session = self.session(ruleset(agent=[{"verdict": "deny", "command": "git push*", "reason": "ask the user first"}]), backend=backend)
        (result, denial), responder = self.run_in(session, "git push origin main")
        self.assertIsNone(result)
        self.assertIn("rule agent:0", denial)
        self.assertIn("ask the user first", denial)
        self.assertEqual((backend.calls, responder.events), ([], []))
        line = self.records()[0]
        self.assertEqual((line["decision"], line["layer"], line["rule"], line["rule_verdict"]), ("denied:rule", "agent", "agent:0", "deny"))

    def test_an_ask_rule_asks_with_its_reason_and_the_audit_keeps_the_approval_id(self) -> None:
        session = self.session(ruleset(user=[{"verdict": "ask", "command": "deploy*", "reason": "production", "id": "deploys"}]))
        (result, denial), responder = self.run_in(session, "deploy now")
        self.assertIsNone(denial)
        card = next(data for kind, data in responder.events if kind == "approval")
        self.assertIn("rule user:deploys", card["reason"])
        self.assertIn("production", card["reason"])
        self.assertEqual(card["risk"], "manual")
        self.assertEqual(result.approval, "[approved by the user]")
        line = self.records()[0]
        self.assertEqual((line["decision"], line["rule"], line["rule_verdict"], line["approval_id"]), ("approved", "user:deploys", "ask", card["id"]))

    def test_a_denied_card_is_audited_with_its_id(self) -> None:
        session = self.session(ruleset(user=[{"verdict": "ask", "command": "x"}]))
        (result, denial), responder = self.run_in(session, "x", self.attended("deny"))
        self.assertIsNone(result)
        self.assertIn("did not approve", denial)
        card = next(data for kind, data in responder.events if kind == "approval")
        line = self.records()[0]
        self.assertEqual((line["decision"], line["approval_id"]), ("denied:denied by user", card["id"]))

    def test_a_dangerous_ask_keeps_its_risk_and_both_reasons(self) -> None:
        session = self.session(ruleset(user=[{"verdict": "ask", "dangerous": True, "reason": "destructive"}]), approvals="off")
        (_, _), responder = self.run_in(session, "rm -rf build")
        card = next(data for kind, data in responder.events if kind == "approval")
        self.assertEqual(card["risk"], "dangerous")
        self.assertIn("destructive", card["reason"])
        self.assertIn("recursive delete", card["reason"])

    def test_unmatched_actions_use_the_layer_default_when_no_shorthand_is_set(self) -> None:
        backend = FakeBackend()
        session = self.session(ruleset(installation={"default": "ask"}, user={"default": "allow"}), backend=backend)
        (result, denial), responder = self.run_in(session, "uname")
        self.assertIsNone(denial)
        self.assertEqual(result.approval, "[ran without approval: allowed by user default]")
        line = self.records()[0]
        self.assertEqual((line["decision"], line["layer"], line["rule_verdict"]), ("auto:default", "user", "allow"))
        self.assertNotIn("rule", line)
        session = self.session(ruleset(user={"default": "deny"}), backend=backend)
        self.assertIn("Denied", self.run_in(session, "uname")[0][1])

    def test_the_shorthand_is_explicit_only_when_the_config_sets_it(self) -> None:
        self.assertIsNone(self.session().shorthand)
        self.assertEqual(self.session(approvals="smart").shorthand, "smart")
        entry = AgentTerminalBackend()
        self.assertNotIn("approvals", entry.model_fields_set)
        copied = entry.model_copy(update={"approvals": "manual"})  # how a worker task pins its backends
        config = AgentTerminalConfig(backends={"box": copied})
        self.assertEqual(config.shorthand("box"), "manual")

    def test_the_shorthand_audit_label_and_notes_are_the_legacy_ones(self) -> None:
        for mode, command, decision, note in (
            ("off", "ls", "auto:off", "[approvals off]"),
            ("smart", "ls", "auto:smart", "[ran without approval: smart mode, normal command]"),
        ):
            with self.subTest(mode=mode):
                self.audit.unlink(missing_ok=True)
                (result, _), _ = self.run_in(self.session(approvals=mode), command)
                self.assertEqual(result.approval, note)
                line = self.records()[0]
                self.assertEqual((line["decision"], line["layer"], line["rule"], line["rule_verdict"]), (decision, "agent", f"approvals:{mode}", "allow"))

    def test_every_segment_of_a_command_is_judged(self) -> None:
        backend = FakeBackend()
        rules = ruleset(user=[{"verdict": "allow", "command": "ls"}, {"verdict": "allow", "command": "cat"}])
        session = self.session(rules, backend=backend)
        (result, denial), responder = self.run_in(session, "ls && cat a")
        self.assertIsNone(denial)
        self.assertEqual(responder.events, [])
        (result, denial), responder = self.run_in(session, "ls && cat a && uname")
        self.assertEqual(responder.kinds(), ["approval", "approval_resolved"])
        self.assertIn("(segment: uname)", next(data for kind, data in responder.events if kind == "approval")["reason"])

    def test_rules_see_the_backend_name_and_kind(self) -> None:
        rules = ruleset(user=[{"verdict": "allow", "tool": "terminal", "backend_kind": "sandbox", "command": "ls"}])
        sandbox = self.session(rules, kind="sandbox")
        machine = self.session(rules, kind="machine")
        self.assertEqual(self.run_in(sandbox, "ls")[1].events, [])
        self.assertEqual(self.run_in(machine, "ls")[1].kinds(), ["approval", "approval_resolved"])
        self.assertEqual(self.session().kind, "machine")
        self.assertEqual(TerminalSession(FakeBackend(), AgentTerminalConfig(backends={"box": AgentTerminalBackend()}),
                                         TerminalSettings(), user_id="u", agent_id="a", name="box", local=True).kind, "local")


class FloorFirstTests(SessionCase):
    def test_hardline_beats_an_allow_everything_rule(self) -> None:
        backend = FakeBackend()
        session = self.session(ruleset(user=[{"verdict": "allow"}], agent={"default": "allow"}), backend=backend)
        (result, denial), responder = self.run_in(session, "rm -rf /")
        self.assertIsNone(result)
        self.assertIn("hardline", denial)
        self.assertEqual((backend.calls, responder.events), ([], []))
        line = self.records()[0]
        self.assertEqual((line["decision"], line["layer"], line["rule"], line["rule_verdict"]), ("hardline", "floor", "hardline", "deny"))

    def test_protected_paths_beat_an_allow_everything_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / ".env"
            key.write_text("SECRET=1", encoding="utf-8")
            backend = FakeBackend()
            session = TerminalSession(
                backend, AgentTerminalConfig(backends={"box": AgentTerminalBackend(workdir=tmp)}), TerminalSettings(),
                user_id="u", agent_id="a", name="box", audit_file=self.audit, local=True,
                rules=ruleset(user=[{"verdict": "allow"}]),
            )
            (result, denial), _ = self.run_in(session, f"cat {key}")
            self.assertIsNone(result)
            self.assertIn("protected path", denial)
            self.assertEqual(backend.calls, [])
            line = self.records()[0]
            self.assertEqual((line["decision"], line["layer"], line["rule"]), ("denied:protected", "floor", "protected"))

    def test_protected_file_tools_beat_an_allow_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text("SECRET=1", encoding="utf-8")
            session = TerminalSession(
                FakeBackend(), AgentTerminalConfig(backends={"box": AgentTerminalBackend(workdir=tmp)}), TerminalSettings(),
                user_id="u", agent_id="a", name="box", audit_file=self.audit, local=True,
                rules=ruleset(user=[{"verdict": "allow"}]),
            )
            with run_context(self.attended().context()):
                self.assertIn("protected path", files.sandbox_read(session, str(env)))
                self.assertIn("protected path", files.sandbox_write(session, str(env), "x"))


class UnattendedTests(SessionCase):
    def test_every_ask_becomes_deny_and_allow_still_runs(self) -> None:
        rules = ruleset(installation={"default": "ask"}, user=[
            {"verdict": "allow", "command": "ls"}, {"verdict": "ask", "command": "deploy*"},
        ])
        for how in ("flag", "no-context", "non-interactive"):
            with self.subTest(how=how):
                self.audit.unlink(missing_ok=True)
                backend = FakeBackend()
                session = self.session(rules, backend=backend, unattended=(how == "flag"))
                if how == "no-context":
                    outcomes = [session.run("terminal", c, c) for c in ("ls", "deploy", "uname")]
                else:
                    with run_context(self.attended().context(interactive=(how != "non-interactive"))):
                        outcomes = [session.run("terminal", c, c) for c in ("ls", "deploy", "uname")]
                self.assertIsNone(outcomes[0][1])
                self.assertIn("nobody is attached", outcomes[1][1])
                self.assertIn("nobody is attached", outcomes[2][1])
                self.assertEqual([c.command for c in backend.calls], ["ls"])
                self.assertEqual([r["decision"] for r in self.records()], ["auto:rule", "denied:unattended", "denied:unattended"])

    def test_unattended_never_asks_a_broker(self) -> None:
        broker = StubBroker()
        session = self.session(ruleset(user=[{"verdict": "ask"}]), unattended=True)
        responder = Responder(broker)  # type: ignore[arg-type]
        with run_context(responder.context()):
            result, denial = session.run("terminal", "ls", "ls")
        self.assertIsNone(result)
        self.assertEqual(broker.requests, [])


class FileToolRuleTests(SessionCase):
    def sandbox(self, rules, **kwargs):
        backend = FakeSandbox({"/srv/data/a.txt": "one\ntwo\n", "~/workspace/b.txt": "hello\n"})
        return self.session(rules, backend=backend, **kwargs), backend

    def test_path_rules_for_reads_writes_and_patches(self) -> None:
        rules = ruleset(installation={"default": "ask"}, user=[
            {"verdict": "allow", "tool": "file_read", "path": "/srv/data/*"},
            {"verdict": "allow", "tool": "file_write", "path": "~/workspace/*"},
            {"verdict": "deny", "tool": "file_write", "path": "/srv/*"},
        ])
        session, backend = self.sandbox(rules)
        responder = self.attended()
        with run_context(responder.context()):
            self.assertIn("one", files.sandbox_read(session, "/srv/data/a.txt"))
            self.assertEqual(responder.events, [])
            self.assertIn("wrote", files.sandbox_write(session, "new.txt", "x"))  # relative: hangs from the workdir
            self.assertEqual(responder.events, [])
            self.assertIn("patched", files.sandbox_patch(session, "~/workspace/b.txt", "hello", "bye"))
            self.assertEqual(responder.events, [])
            self.assertIn("rule user:2", files.sandbox_write(session, "/srv/data/a.txt", "no"))
            self.assertEqual(responder.events, [])
        self.assertEqual(backend.files["~/workspace/b.txt"], "bye\n")

    def test_unmatched_paths_ask_and_search_is_a_read(self) -> None:
        rules = ruleset(installation={"default": "ask"}, user=[{"verdict": "allow", "tool": "file_read", "path": "/srv/data"}])
        session, _ = self.sandbox(rules)
        responder = self.attended()
        with run_context(responder.context()):
            files.sandbox_search(session, "one", "/srv/data")
            self.assertEqual(responder.events, [])
            files.sandbox_read(session, "/srv/other.txt")
        self.assertEqual(responder.kinds(), ["approval", "approval_resolved"])
        self.assertEqual(next(d for k, d in responder.events if k == "approval")["tool"], "sandbox_read")

    def test_file_write_dangerous_system_paths_match_dangerous_rules(self) -> None:
        rules = ruleset(user=[{"verdict": "allow", "tool": "file_write", "dangerous": False}, {"verdict": "ask", "dangerous": True}],
                        installation={"default": "deny"})
        session, _ = self.sandbox(rules)
        responder = self.attended()
        with run_context(responder.context()):
            self.assertIn("wrote", files.sandbox_write(session, "/tmp/x", "x"))
            self.assertEqual(responder.events, [])
            files.sandbox_write(session, "/etc/hosts", "x")
        card = next(d for k, d in responder.events if k == "approval")
        self.assertEqual(card["risk"], "dangerous")

    def test_a_patch_asks_once(self) -> None:
        session, _ = self.sandbox(ruleset(user=[{"verdict": "ask", "tool": "file_write"}]))
        responder = self.attended()
        with run_context(responder.context()):
            files.sandbox_patch(session, "~/workspace/b.txt", "hello", "bye")
        self.assertEqual(responder.kinds().count("approval"), 1)

    def test_unattended_file_write_ask_is_denied_but_read_allow_runs(self) -> None:
        rules = ruleset(user=[{"verdict": "allow", "tool": "file_read"}, {"verdict": "ask", "tool": "file_write"}])
        session, backend = self.sandbox(rules, unattended=True)
        self.assertIn("one", files.sandbox_read(session, "/srv/data/a.txt"))
        self.assertIn("nobody is attached", files.sandbox_write(session, "/srv/data/a.txt", "x"))
        self.assertEqual(backend.files["/srv/data/a.txt"], "one\ntwo\n")


class AuditFailClosedTests(SessionCase):
    def test_an_allow_rule_does_not_run_when_the_audit_cannot_be_written(self) -> None:
        backend = FakeBackend()
        session = self.session(ruleset(user=[{"verdict": "allow"}]), backend=backend)
        session.audit_file = self.audit.parent  # a directory: not writable as a file
        with run_context(self.attended().context()):
            result, text = session.run("terminal", "ls", "ls")
        self.assertIsNone(result)
        self.assertEqual(text, AUDIT_FAILED)
        self.assertEqual(backend.calls, [])


class BuildToolsTests(unittest.TestCase):
    """`build_terminal_tools` hands the three layers and the backend kind to every session."""

    def build(self, agent, installation=None, backend=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        for target, value in (
            ("src.agent.toolsets.terminal.DATA_DIR", self.data_dir),
            ("src.agent.toolsets.terminal.APPROVAL_RULES", ApprovalRuleList.model_validate(installation or {})),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return {t.__name__: t for t in build_terminal_tools(agent, backends={"box": backend or FakeBackend()})}

    def agent(self, approvals=None, **layers):
        base = resolved_agent("manual")
        entry = AgentTerminalBackend(workdir="~/workspace", **({"approvals": approvals} if approvals else {}))
        terminal = AgentTerminalConfig(backends={"box": entry}, default_backend="box")
        sandbox = ssh_config(kind="sandbox")
        return base.model_copy(update={"terminal": terminal, "terminal_backends": {"box": sandbox}, **layers})

    def run_tool(self, tools, command, answer="approve"):
        responder = Responder(ApprovalBroker(2), answer=answer)
        with run_context(responder.context()):
            return tools["terminal"](command), responder

    def test_the_three_layers_and_the_kind_reach_the_session(self) -> None:
        agent = self.agent(
            user_approval_rules=ApprovalRuleList.model_validate({"rules": [{"verdict": "allow", "command": "ls", "backend_kind": "sandbox"}]}),
            approval_rules=ApprovalRuleList.model_validate({"rules": [{"verdict": "deny", "command": "ls /secret*"}]}),
        )
        tools = self.build(agent, installation={"rules": [{"verdict": "allow", "command": "pwd"}]})
        text, responder = self.run_tool(tools, "ls")
        self.assertTrue(text.startswith("[ran without approval: allowed by user:0]"), text)
        text, _ = self.run_tool(tools, "pwd")
        self.assertTrue(text.startswith("[ran without approval: allowed by installation:0]"), text)
        text, responder = self.run_tool(tools, "ls /secret/x")
        self.assertIn("rule agent:0", text)
        text, responder = self.run_tool(tools, "uname")
        self.assertEqual(responder.kinds(), ["approval", "approval_resolved"])
        lines = [json.loads(l) for l in (self.data_dir / "u" / "a" / "terminal_audit.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([l["layer"] for l in lines], ["user", "installation", "agent", "builtin"])

    def test_the_legacy_shorthand_still_pins_the_backend(self) -> None:
        agent = self.agent(approvals="smart", user_approval_rules=ApprovalRuleList(default="deny"))
        tools = self.build(agent)
        text, responder = self.run_tool(tools, "ls")
        self.assertTrue(text.startswith("[ran without approval: smart mode, normal command]"), text)
        self.assertEqual(responder.events, [])

    def test_without_a_shorthand_the_defaults_decide(self) -> None:
        agent = self.agent(approval_rules=ApprovalRuleList(default="allow"))
        tools = self.build(agent, installation={"default": "ask"})
        text, _ = self.run_tool(tools, "ls")
        self.assertTrue(text.startswith("[ran without approval: allowed by agent default]"), text)

    def test_the_notice_describes_the_effective_mode(self) -> None:
        self.build(self.agent())
        self.assertIn("approvals: every command and file operation asks first", terminal_notice(self.agent(approvals="manual")))
        allowing = self.agent(approvals="manual", approval_rules=ApprovalRuleList(rules=[ApprovalRule(verdict="allow", tool="terminal", command="ls *")]))
        self.assertIn("approval rules may allow some actions without asking", terminal_notice(allowing))
        notice = terminal_notice(self.agent())
        self.assertIn("approval rules decide", notice)
        self.assertIn("ask first unless a rule allows them", notice)
        allow = terminal_notice(self.agent(approval_rules=ApprovalRuleList(default="allow")))
        self.assertIn("actions run without asking unless a rule says otherwise", allow)


class LoadTests(AdminFixture):
    """The layers come from user.yaml and the agent file; the guardrail runs when an agent loads."""

    BACKENDS = (
        "terminal_backends:\n"
        "  sandbox: {type: ssh, kind: sandbox, host: a.example, user: deploy, key_file: /k, known_hosts_file: /h}\n"
        "  pc: {type: ssh, host: b.example, user: user-a, key_file: /k, known_hosts_file: /h}\n"
    )
    AGENT = "id: bot\nname: Bot\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: {default}\n  backends: {{{backends}}}\n{extra}"

    def setUp(self) -> None:
        super().setUp()
        environment = patch.dict(os.environ, {"USER_A_KEY_NAME": "secret"})
        environment.start()
        self.addCleanup(environment.stop)

    def setup_files(self, *, user_extra="", backends="sandbox: {}", extra="", level="operator") -> None:
        from tests.support.admin_helpers import USER_YAML
        from tests.support.auth_helpers import TEST_PASSWORD_HASH

        user = USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("max_level: admin", f"max_level: {level}")
        self.write_user(user + self.BACKENDS + user_extra)
        self.write_agent("bot", self.AGENT.format(backends=backends, extra=extra, default=backends.split(":")[0]))

    def test_both_layers_are_loaded(self) -> None:
        self.setup_files(
            user_extra="approval_rules:\n  rules: [{verdict: allow, command: ls}]\n",
            extra="approval_rules:\n  default: deny\n",
        )
        resolved = load_agent("user-a", "bot")
        self.assertEqual(resolved.user_approval_rules.rules[0].command, "ls")
        self.assertEqual(resolved.approval_rules.default, "deny")
        self.assertEqual(resolved.terminal_backends["sandbox"].backend_kind, "sandbox")
        self.assertEqual(resolved.level, Level.OPERATOR)

    def test_an_invalid_rule_is_a_load_error_that_names_the_field(self) -> None:
        from src.config.agents import AgentConfigError

        self.setup_files(extra="approval_rules:\n  rules: [{verdict: allow, comand: ls}]\n")
        with self.assertRaises(AgentConfigError) as raised:
            load_agent("user-a", "bot")
        self.assertIn("Extra inputs are not permitted", str(raised.exception))
        self.setup_files(user_extra="approval_rules:\n  rules: [{verdict: sometimes}]\n")
        with self.assertRaises(Exception) as raised:
            load_agent("user-a", "bot")
        self.assertIn("verdict", str(raised.exception))

    def test_off_on_a_machine_needs_admin_but_a_sandbox_does_not(self) -> None:
        self.setup_files(backends="sandbox: {approvals: 'off'}")
        self.assertEqual(load_agent("user-a", "bot").terminal.backends["sandbox"].approvals, "off")
        self.setup_files(backends="pc: {approvals: 'off'}", extra="level: operator\n")
        with self.assertRaises(ApprovalRuleNotAllowed) as raised:
            load_agent("user-a", "bot")
        self.assertEqual(raised.exception.code, "approval_rule_not_allowed")
        self.assertIn("backend 'pc' (machine)", str(raised.exception))
        self.setup_files(backends="pc: {approvals: 'off'}", extra="level: admin\n", level="admin")
        load_agent("user-a", "bot")

    def test_a_blanket_user_rule_breaks_only_agents_below_admin_that_use_a_machine(self) -> None:
        self.setup_files(user_extra="approval_rules:\n  rules: [{verdict: allow}]\n", backends="pc: {}", extra="level: operator\n")
        with self.assertRaises(ApprovalRuleNotAllowed):
            load_agent("user-a", "bot")
        self.setup_files(user_extra="approval_rules:\n  rules: [{verdict: allow}]\n", backends="sandbox: {}", extra="level: operator\n")
        load_agent("user-a", "bot")

    def test_the_guardrail_runs_in_the_config_check(self) -> None:
        from src.config.levels import check_agent_level
        from src.config.agents import AgentStore

        self.setup_files(backends="pc: {approvals: 'off'}", extra="level: operator\n")
        store = AgentStore(self.config_dir, self.data_dir)
        with self.assertRaises(ApprovalRuleNotAllowed):
            check_agent_level(store.load("user-a", "bot"), store.user("user-a"), Level.ADMIN)


if __name__ == "__main__":
    unittest.main()
