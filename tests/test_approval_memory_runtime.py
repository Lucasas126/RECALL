"""Approval memory at run time: the card's scopes, the session grants, the agent rule written to the yaml, audit, pickup."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("RECALL_SESSION_SECRET", "x")

import yaml
from admin_helpers import USER_YAML, AdminFixture, FakeAgent
from auth_helpers import TEST_PASSWORD_HASH
from terminal_helpers import FakeBackend, FakeSandbox

from src.config.levels import ApprovalRuleNotAllowed
from src.config.loader import load_agent, load_agent_config
from src.config.settings import APPROVAL_RULES
from src.config.models import TerminalSettings
from src.providers.cancellation import CancellationToken
from src.service.chat import ChatService
from src.terminal import files
from src.terminal.approvals import ApprovalBroker, ApprovalForbidden
from src.terminal.audit import audit_path
from src.terminal.context import RunContext, run_context
from src.terminal.rules import LEARNED_REASON, RuleSet
from src.terminal.session import TerminalSession
from src.utils.i18n import InputError

BACKENDS = (
    "terminal_backends:\n"
    "  sandbox: {type: ssh, kind: sandbox, host: a.example, user: deploy, key_file: /k, known_hosts_file: /h}\n"
    "  pc: {type: ssh, host: b.example, user: user-a, key_file: /k, known_hosts_file: /h}\n"
)
AGENT = (
    "id: bot\nname: Bot\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: sandbox\n"
    "  backends: {sandbox: {}}\n"
)


class Asker:
    """A chat run that answers every approval card through the service, the way the web API does."""

    def __init__(self, service: ChatService, chat_id: str = "chat1", answers=(("approve", "once"),), user: str = "user-a"):
        self.service, self.chat_id, self.user = service, chat_id, user
        self.answers = list(answers)
        self.events: list[tuple[str, dict]] = []
        self.results: list[dict] = []

    def emit(self, kind: str, data: dict) -> None:
        self.events.append((kind, data))
        if kind == "approval":
            decision, scope = self.answers.pop(0) if self.answers else ("deny", "once")
            self.results.append(self.service.resolve_approval(self.user, "bot", self.chat_id, data["id"], decision, scope))

    @property
    def cards(self) -> list[dict]:
        return [data for kind, data in self.events if kind == "approval"]

    def context(self, *, learn: bool = True) -> RunContext:
        return RunContext(
            "user-a", "bot", self.chat_id, self.service._approvals, self.emit, CancellationToken(), interactive=True,
            learn_rules=(lambda target, rules: self.service.learn_approval_rules("user-a", target, rules, chat_id=self.chat_id)) if learn else None,
        )


class MemoryFixture(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        environment = patch.dict(os.environ, {"USER_A_KEY_NAME": "secret"})
        environment.start()
        self.addCleanup(environment.stop)
        self.write_user(USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("max_level: admin", "max_level: operator") + BACKENDS)
        self.write_agent("bot", AGENT)
        self.audit = audit_path(self.data_dir, "user-a", "bot")

    def session(self, backend=None, *, rules: RuleSet | None = None) -> TerminalSession:
        resolved = load_agent("user-a", "bot")
        rules = rules or RuleSet.build(APPROVAL_RULES, resolved.user_approval_rules, resolved.approval_rules)
        return TerminalSession(
            backend or FakeBackend(), resolved.terminal, TerminalSettings(), user_id="user-a", agent_id="bot",
            name="sandbox", audit_file=self.audit, rules=rules, kind="sandbox",
        )

    def run_cmd(self, session, asker, command):
        with run_context(asker.context()):
            return session.run("terminal", command, command)

    def audit_lines(self) -> list[dict]:
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()] if self.audit.exists() else []

    def agent_rules(self) -> list[dict]:
        rules = load_agent_config("user-a", "bot").approval_rules
        return [rule.model_dump(exclude_none=True) for rule in rules.rules] if rules else []


class ScopeTests(MemoryFixture):
    def test_the_card_lists_the_allowed_scopes(self) -> None:
        asker = Asker(self.service)
        self.run_cmd(self.session(), asker, "ls -l")
        self.assertEqual(asker.cards[0]["options"], ["once", "session", "agent"])

    def test_a_card_without_a_rule_writer_does_not_offer_agent(self) -> None:
        asker = Asker(self.service)
        with run_context(asker.context(learn=False)):
            self.session().run("terminal", "ls", "ls")
        self.assertEqual(asker.cards[0]["options"], ["once", "session"])

    def test_once_asks_again_next_time_and_writes_nothing(self) -> None:
        asker = Asker(self.service, answers=[("approve", "once")] * 2)
        session = self.session()
        for _ in range(2):
            result, denial = self.run_cmd(session, asker, "ls -l")
            self.assertIsNone(denial)
        self.assertEqual(len(asker.cards), 2)
        self.assertEqual(self.agent_rules(), [])
        self.assertEqual([line["scope"] for line in self.audit_lines()], ["once", "once"])
        self.assertEqual(asker.results, [{"status": "approved", "scope": "once"}] * 2)

    def test_deny_is_audited_with_its_scope_and_ignores_the_scope_sent(self) -> None:
        asker = Asker(self.service, answers=[("deny", "agent")])
        result, denial = self.run_cmd(self.session(), asker, "ls")
        self.assertIsNone(result)
        self.assertEqual(asker.results, [{"status": "denied", "scope": "deny"}])
        self.assertEqual(self.agent_rules(), [])
        self.assertEqual(self.audit_lines()[0]["scope"], "deny")

    def test_session_remembers_the_exact_action_only(self) -> None:
        asker = Asker(self.service, answers=[("approve", "session")])
        session = self.session()
        first, _ = self.run_cmd(session, asker, "ls -l")
        second, denial = self.run_cmd(session, asker, "ls -l")
        self.assertIsNone(denial)
        self.assertEqual(len(asker.cards), 1)  # the second run was not asked
        self.assertEqual(second.approval, "[approved by the user earlier in this session]")
        other = Asker(self.service, answers=[("deny", "once")])
        result, _ = self.run_cmd(session, other, "ls -l /etc")  # another command is asked again
        self.assertIsNone(result)
        self.assertEqual(len(other.cards), 1)
        lines = self.audit_lines()
        self.assertEqual([(line["decision"], line["scope"]) for line in lines[:2]], [("approved", "session"), ("approved:session", "session")])
        self.assertNotIn("approval_id", lines[1])
        self.assertEqual(self.agent_rules(), [])  # nothing written to the yaml

    def test_session_memory_is_isolated_between_chats_and_agents_of_the_run(self) -> None:
        first = Asker(self.service, "chat1", [("approve", "session")])
        session = self.session()
        self.run_cmd(session, first, "pwd")
        second = Asker(self.service, "chat2", [("deny", "once")])
        result, _ = self.run_cmd(session, second, "pwd")  # same action, another chat: asked again
        self.assertIsNone(result)
        self.assertEqual(len(second.cards), 1)
        self.assertTrue(self.service._approvals.session_allows("user-a", "bot", "chat1", self.signature("pwd")))
        self.assertFalse(self.service._approvals.session_allows("user-a", "bot", "chat2", self.signature("pwd")))
        self.assertFalse(self.service._approvals.session_allows("user-a", "other-agent", "chat1", self.signature("pwd")))
        self.service._approvals.forget_chat("user-a", "bot", "chat1")
        self.assertFalse(self.service._approvals.session_allows("user-a", "bot", "chat1", self.signature("pwd")))

    @staticmethod
    def signature(command: str) -> str:
        from src.terminal.rules import Action, action_signature

        return action_signature(Action("terminal", command, "", "sandbox", "sandbox"))

    def test_deleting_the_chat_drops_its_session_grants(self) -> None:
        chat = self.service.create_chat("user-a", "bot")["id"]
        self.run_cmd(self.session(), Asker(self.service, chat, [("approve", "session")]), "pwd")
        self.assertTrue(self.service._approvals.session_allows("user-a", "bot", chat, self.signature("pwd")))
        self.service.delete_chat("user-a", "bot", chat)
        self.assertFalse(self.service._approvals.session_allows("user-a", "bot", chat, self.signature("pwd")))

    def test_session_grants_are_per_signature_for_file_operations(self) -> None:
        sandbox = FakeSandbox({"/srv/a.txt": "x"})
        session = self.session(sandbox)
        asker = Asker(self.service, answers=[("approve", "session")])
        with run_context(asker.context()):
            files.sandbox_read(session, "/srv/a.txt")
            self.assertIn("1|x", files.sandbox_read(session, "/srv/a.txt"))
        self.assertEqual(len(asker.cards), 1)
        self.assertEqual(asker.cards[0]["options"], ["once", "session", "agent"])


class AgentScopeTests(MemoryFixture):
    def test_always_for_this_agent_writes_a_narrow_rule_and_the_next_call_is_not_asked(self) -> None:
        asker = Asker(self.service, answers=[("approve", "agent")])
        session = self.session()
        result, denial = self.run_cmd(session, asker, "git status -s")
        self.assertIsNone(denial)
        self.assertEqual(asker.results, [{"status": "approved", "scope": "agent"}])
        self.assertEqual(self.agent_rules(), [{
            "verdict": "allow", "tool": "terminal", "command": "git status -s", "backend": "sandbox", "exact": True,
            "reason": LEARNED_REASON,
        }])
        # the live run picks the rule up: the same command in the same reply is not asked again
        again, denial = self.run_cmd(session, asker, "git status -s")
        self.assertIsNone(denial)
        self.assertEqual(len(asker.cards), 1)
        self.assertIn("allowed by agent:0", again.approval)
        # ...but a different command is
        other = Asker(self.service, answers=[("deny", "once")])
        self.run_cmd(session, other, "git status")
        self.assertEqual(len(other.cards), 1)
        lines = self.audit_lines()
        self.assertEqual([line.get("scope") for line in lines if line.get("tool")], ["agent", None, "deny"])
        self.assertEqual([line["decision"] for line in lines if line.get("tool")], ["approved", "auto:rule", "denied:denied by user"])

    def test_a_new_session_built_from_the_file_has_the_rule(self) -> None:
        self.run_cmd(self.session(), Asker(self.service, answers=[("approve", "agent")]), "uptime")
        fresh = Asker(self.service)
        result, denial = self.run_cmd(self.session(), fresh, "uptime")  # rules come from the yaml again
        self.assertIsNone(denial)
        self.assertEqual(fresh.cards, [])
        self.assertEqual(load_agent("user-a", "bot").approval_rules.rules[0].command, "uptime")

    def test_a_multi_segment_command_stores_each_segment(self) -> None:
        asker = Asker(self.service, answers=[("approve", "agent")])
        self.run_cmd(self.session(), asker, "cd /srv && ls -l")
        self.assertEqual([rule["command"] for rule in self.agent_rules()], ["cd /srv", "ls -l"])
        fresh = Asker(self.service)
        self.run_cmd(self.session(), fresh, "ls -l")  # a segment alone is allowed too
        self.assertEqual(fresh.cards, [])

    def test_file_operations_store_the_exact_path_and_kind(self) -> None:
        sandbox = FakeSandbox()
        asker = Asker(self.service, answers=[("approve", "agent")])
        with run_context(asker.context()):
            files.sandbox_write(self.session(sandbox), "/srv/out.txt", "hello")
        self.assertEqual(self.agent_rules(), [{
            "verdict": "allow", "tool": "file_write", "path": "/srv/out.txt", "backend": "sandbox", "exact": True,
            "reason": LEARNED_REASON,
        }])
        fresh = Asker(self.service)
        with run_context(fresh.context()):
            files.sandbox_write(self.session(sandbox), "/srv/out.txt", "again")
            files.sandbox_write(self.session(sandbox), "/srv/other.txt", "x")  # another path: asked (denied by default answer)
        self.assertEqual(len(fresh.cards), 1)
        self.assertEqual(fresh.cards[0]["command"].splitlines()[0].split()[1], "/srv/other.txt")

    def test_identical_rules_are_not_duplicated(self) -> None:
        session = self.session()
        self.run_cmd(session, Asker(self.service, answers=[("approve", "agent")]), "id")
        # a session built before the rule existed asks again; answering 'agent' again must not add a second copy
        stale = self.session(rules=RuleSet())
        self.run_cmd(stale, Asker(self.service, answers=[("approve", "agent")]), "id")
        self.assertEqual(len(self.agent_rules()), 1)
        self.assertEqual([line["action"] for line in self.audit_lines() if line.get("event") == "approval_rule"], ["add"])

    def test_the_rule_write_is_audited_with_actor_agent_and_rule(self) -> None:
        self.run_cmd(self.session(), Asker(self.service, "chat9", [("approve", "agent")]), "date")
        line = next(line for line in self.audit_lines() if line.get("event") == "approval_rule")
        self.assertEqual((line["action"], line["actor"], line["user"], line["agent"], line["chat"]), ("add", "user", "user-a", "bot", "chat9"))
        self.assertEqual(line["rule"]["command"], "date")
        self.assertEqual(line["rule"]["backend"], "sandbox")
        self.assertIn("ts", line)

    def test_the_guardrail_failure_keeps_the_one_time_approval_with_a_clear_error(self) -> None:
        before = self.read("user-a/agents/bot.yaml")
        failing = ApprovalRuleNotAllowed("a blanket allow is not allowed: {problem}", problem="test")
        asker = Asker(self.service, answers=[("approve", "agent")])
        backend = FakeBackend()
        with patch.object(self.service._agents, "_check_level", side_effect=failing):
            result, denial = self.run_cmd(self.session(backend), asker, "ls")
        self.assertIsNone(denial)  # the command still ran, approved once
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(asker.results[0]["status"], "approved")
        self.assertEqual(asker.results[0]["scope"], "once")
        self.assertIn("blanket allow", asker.results[0]["error"])
        self.assertEqual(self.read("user-a/agents/bot.yaml"), before)  # nothing written
        self.assertEqual([line["scope"] for line in self.audit_lines() if line.get("tool")], ["once"])
        self.assertFalse([line for line in self.audit_lines() if line.get("event")])

    def test_an_unwritable_audit_trail_keeps_the_one_time_approval(self) -> None:
        asker = Asker(self.service, answers=[("approve", "agent")])
        with patch("src.service.chat.audit_writable", return_value=False):
            self.run_cmd(self.session(), asker, "ls")
        self.assertEqual(asker.results[0]["scope"], "once")
        self.assertIn("audit", asker.results[0]["error"])
        self.assertEqual(self.agent_rules(), [])

    def test_a_scope_the_card_did_not_offer_is_refused_and_the_card_stays_open(self) -> None:
        seen: list = []

        def emit(kind, data):
            if kind != "approval":
                return
            self.assertEqual(data["options"], ["once"])
            with self.assertRaises(InputError):
                self.service.resolve_approval("user-a", "bot", "chat1", data["id"], "approve", "agent")
            seen.append(self.service._approvals.get(data["id"]) is not None)  # still pending after the refusal
            self.service.resolve_approval("user-a", "bot", "chat1", data["id"], "approve", "once")

        context = RunContext("user-a", "bot", "chat1", self.service._approvals, emit, CancellationToken(), interactive=True,
                             learn_rules=lambda *a: self.fail("must not write"))
        with run_context(context):
            result, denial = self.session().run("terminal", "rm -rf build", "rm -rf build")  # dangerous: only once
        self.assertIsNone(denial)
        self.assertEqual(seen, [True])
        self.assertEqual(self.agent_rules(), [])

    def test_dangerous_actions_offer_only_once(self) -> None:
        asker = Asker(self.service, answers=[("approve", "once")])
        self.run_cmd(self.session(), asker, "rm -rf build")
        self.assertEqual((asker.cards[0]["risk"], asker.cards[0]["options"]), ("dangerous", ["once"]))

    def test_a_deny_rule_is_never_asked_so_no_always_is_offered(self) -> None:
        self.write_agent("bot", AGENT + "approval_rules:\n  rules: [{verdict: deny, command: 'ls *', reason: nope}]\n")
        asker = Asker(self.service)
        result, denial = self.run_cmd(self.session(), asker, "ls -l")
        self.assertIsNone(result)
        self.assertEqual(asker.cards, [])

    def test_an_ask_rule_offers_session_but_not_agent(self) -> None:
        self.write_agent("bot", AGENT + "approval_rules:\n  rules: [{verdict: ask, command: deploy}]\n")
        asker = Asker(self.service)
        self.run_cmd(self.session(), asker, "deploy now")
        self.assertEqual(asker.cards[0]["options"], ["once", "session"])

    def test_the_floor_still_wins_over_a_learned_rule(self) -> None:
        # a rule for a hardline command can only be written by hand; the floor is checked before any rule
        self.write_agent("bot", AGENT + "approval_rules:\n  rules: [{verdict: allow, command: 'rm -rf /', exact: true}]\n")
        backend = FakeBackend()
        asker = Asker(self.service)
        result, denial = self.run_cmd(self.session(backend), asker, "rm -rf /")
        self.assertIsNone(result)
        self.assertIn("hardline", denial)
        self.assertEqual(backend.calls, [])

    def test_only_the_owner_can_answer(self) -> None:
        errors: list = []

        def emit(kind, data):
            if kind != "approval":
                return
            with self.assertRaises(ApprovalForbidden):
                self.service._approvals.answer(data["id"], "intruder", "approve", "agent")
            errors.append("refused")
            self.service.resolve_approval("user-a", "bot", "chat1", data["id"], "deny")

        context = RunContext("user-a", "bot", "chat1", self.service._approvals, emit, CancellationToken(), interactive=True,
                             learn_rules=lambda target, rules: self.service.learn_approval_rules("user-a", target, rules))
        with run_context(context):
            result, _ = self.session().run("terminal", "ls", "ls")
        self.assertIsNone(result)
        self.assertEqual(errors, ["refused"])
        self.assertEqual(self.agent_rules(), [])

    def test_unattended_runs_never_reach_the_card(self) -> None:
        asker = Asker(self.service)
        context = asker.context()
        context.interactive = False
        with run_context(context):
            result, denial = self.session().run("terminal", "ls", "ls")
        self.assertIsNone(result)
        self.assertEqual(asker.cards, [])

    def test_answering_twice_writes_one_rule(self) -> None:
        def emit(kind, data):
            if kind == "approval":
                for _ in range(2):
                    self.service.resolve_approval("user-a", "bot", "chat1", data["id"], "approve", "agent")

        context = RunContext("user-a", "bot", "chat1", self.service._approvals, emit, CancellationToken(), interactive=True,
                             learn_rules=lambda target, rules: self.service.learn_approval_rules("user-a", target, rules))
        with run_context(context):
            self.session().run("terminal", "whoami", "whoami")
        self.assertEqual(len(self.agent_rules()), 1)


class PickupTests(MemoryFixture):
    def test_the_cached_agent_is_rebuilt_after_a_rule_is_learned(self) -> None:
        built: list[str] = []

        def factory(user_id, agent_id, chat_id):
            rules = load_agent_config(user_id, agent_id).approval_rules
            built.append(str(len(rules.rules) if rules else 0))
            return FakeAgent("x")

        service = ChatService(agent_factory=factory)
        chat = service.create_chat("user-a", "bot")["id"]

        def say():
            list(service.send("user-a", "bot", chat, "hi"))
            for slot in service._slots.values():
                if slot.worker is not None:
                    slot.worker.join(3)

        say()
        say()
        self.assertEqual(built, ["0"])  # unchanged config: the cached runtime
        from src.terminal.rules import Action, rules_for_action

        service.learn_approval_rules("user-a", "bot", rules_for_action(Action("terminal", "ls", "", "sandbox", "sandbox")))
        say()
        self.assertEqual(built, ["0", "1"])  # rebuilt between replies with the new rule


if __name__ == "__main__":
    unittest.main()
