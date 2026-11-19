"""Approval rules changed on disk (permission tools, web form, learned rules) reach a session that already exists."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from tests.support.admin_helpers import AdminFixture
from tests.support.terminal_helpers import FakeBackend, Responder, resolved_agent

from src.agent.toolsets.terminal import build_terminal_tools
from src.config.agents import AgentStore
from src.terminal.approvals import ApprovalBroker
from src.terminal.context import run_context
from src.terminal.rules import ApprovalRule

ALLOW_LS = "approval_rules:\n  rules:\n    - {verdict: allow, tool: terminal, command: ls}\n"
DENY_LS = "approval_rules:\n  rules:\n    - {verdict: deny, tool: terminal, command: ls}\n"


class LiveRulesTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        patcher = patch("src.agent.toolsets.terminal.CONFIG_DIR", self.config_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        data = patch("src.agent.toolsets.terminal.DATA_DIR", self.data_dir)
        data.start()
        self.addCleanup(data.stop)
        audit = patch("src.agent.toolsets.terminal.audit_path", lambda *a: self.root / "audit.jsonl")
        audit.start()
        self.addCleanup(audit.stop)
        self.write_agent("bot", "id: bot\nname: Bot\n")
        config = resolved_agent("manual").model_copy(update={"user_id": "user-a", "id": "bot"})
        self.backend = FakeBackend()
        tools = build_terminal_tools(config, backends={"box": self.backend})
        self.terminal = next(tool for tool in tools if tool.__name__ == "terminal")
        self.session = self.terminal.sessions["box"]

    def call(self, command: str, answer: str = "approve") -> tuple[str, Responder]:
        responder = Responder(ApprovalBroker(2), answer=answer, user="user-a")
        with run_context(responder.context()):
            return self.terminal(command=command), responder

    def asked(self, responder: Responder) -> bool:
        return "approval" in responder.kinds()

    def test_a_rule_added_mid_turn_is_honoured_by_the_next_decision(self) -> None:
        _, responder = self.call("ls")
        self.assertTrue(self.asked(responder))
        self.write_agent("bot", "id: bot\nname: Bot\n" + ALLOW_LS)
        out, responder = self.call("ls")
        self.assertFalse(self.asked(responder))
        self.assertIn("allowed by agent:0", out)

    def test_a_deny_added_mid_turn_blocks(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\n" + ALLOW_LS)
        self.assertIn("allowed by", self.call("ls")[0])
        self.write_agent("bot", "id: bot\nname: Bot\n" + DENY_LS)
        calls = len(self.backend.calls)
        out, responder = self.call("ls")
        self.assertIn("Denied", out)
        self.assertFalse(self.asked(responder))
        self.assertEqual(len(self.backend.calls), calls)

    def test_removing_and_resetting_rules_apply(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\n" + DENY_LS)
        self.assertIn("Denied", self.call("ls")[0])
        store = AgentStore(self.config_dir, self.data_dir)
        store.remove_approval_rule("user-a", "bot", 0)
        _, responder = self.call("ls")
        self.assertTrue(self.asked(responder))
        store.add_approval_rules("user-a", "bot", [ApprovalRule(verdict="allow", tool="terminal", command="ls")])
        self.assertFalse(self.asked(self.call("ls")[1]))
        store.clear_approval_rules("user-a", "bot")
        self.assertTrue(self.asked(self.call("ls")[1]))

    def test_a_user_layer_change_applies_too(self) -> None:
        text = self.read("user-a/user.yaml") + "approval_rules:\n  rules:\n    - {verdict: deny, tool: terminal, command: ls}\n"
        self.write_user(text)
        self.assertIn("Denied", self.call("ls")[0])

    def test_invalid_yaml_keeps_the_last_good_rules(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\n" + DENY_LS)
        self.assertIn("Denied", self.call("ls")[0])
        for broken in ("id: bot\nname: [unclosed\n", "id: bot\napproval_rules: {rules: [{verdict: maybe}]}\n", ""):
            self.write_agent("bot", broken)
            out, responder = self.call("ls")
            self.assertIn("Denied", out)
            self.assertFalse(self.asked(responder))
        self.write_agent("bot", "id: bot\nname: Bot\n")  # a good file replaces the last good set again
        self.assertTrue(self.asked(self.call("ls")[1]))

    def test_a_missing_file_keeps_the_last_good_rules(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\n" + DENY_LS)
        self.assertIn("Denied", self.call("ls")[0])
        (self.config_dir / "user-a" / "agents" / "bot.yaml").unlink()
        self.assertIn("Denied", self.call("ls")[0])

    def test_a_learned_rule_works_at_once_and_is_not_duplicated_once_persisted(self) -> None:
        rule = ApprovalRule(verdict="allow", tool="terminal", command="ls", exact=True)
        self.session.add_rules([rule])
        self.assertEqual(len(self.session.rules.layer("agent").rules), 1)
        self.session.add_rules([rule])
        self.assertEqual(len(self.session.rules.layer("agent").rules), 1)
        AgentStore(self.config_dir, self.data_dir).add_approval_rules("user-a", "bot", [rule])
        self.assertEqual(len(self.session.rules.layer("agent").rules), 1)
        self.assertEqual(len(self.session.rules.layer("agent").rules), 1)

    def test_a_reset_removes_a_learned_rule_that_was_persisted(self) -> None:
        rule = ApprovalRule(verdict="allow", tool="terminal", command="ls", exact=True)
        store = AgentStore(self.config_dir, self.data_dir)
        store.add_approval_rules("user-a", "bot", [rule])
        self.session.add_rules([rule])
        self.assertFalse(self.asked(self.call("ls")[1]))
        store.clear_approval_rules("user-a", "bot")
        self.assertTrue(self.asked(self.call("ls")[1]))

    def test_the_unchanged_files_are_not_parsed_again(self) -> None:
        self.session.rules  # noqa: B018
        with patch.object(AgentStore, "load", side_effect=AssertionError("re-read")):
            self.session.rules  # noqa: B018
            self.session.rules  # noqa: B018


if __name__ == "__main__":
    unittest.main()
