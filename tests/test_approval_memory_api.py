"""Approval memory management: the rules an agent learned (store, web API, admin CLI), the audit lines and the reset."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("RECALL_SESSION_SECRET", "x")

import yaml
from admin_helpers import USER_YAML, AdminFixture
from auth_helpers import TEST_PASSWORD_HASH, login
from fastapi.testclient import TestClient

from src.config.agents import UnknownApprovalRule
from src.config.levels import ApprovalRuleNotAllowed
from src.config.loader import load_agent_config
from src.terminal.audit import audit_path
from src.terminal.rules import LEARNED_REASON, Action, ApprovalRule, rules_for_action
from src.web.app import create_app

BACKENDS = (
    "terminal_backends:\n"
    "  sandbox: {type: ssh, kind: sandbox, host: a.example, user: deploy, key_file: /k, known_hosts_file: /h}\n"
    "  pc: {type: ssh, host: b.example, user: user-a, key_file: /k, known_hosts_file: /h}\n"
)
AGENT = (
    "id: bot\nname: Bot\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: sandbox\n"
    "  backends: {sandbox: {}}\n"
)
HAND_WRITTEN = "approval_rules:\n  default: ask\n  rules:\n    - {verdict: deny, command: 'rm *', id: nope}\n"


def learned(command: str, backend: str = "sandbox") -> ApprovalRule:
    return rules_for_action(Action("terminal", command, "", backend, "sandbox"))[0]


class RulesFixture(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        os.environ.setdefault("USER_A_KEY_NAME", "secret")
        self.write_user(USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("max_level: admin", "max_level: operator") + BACKENDS)
        self.write_agent("bot", AGENT)
        self.audit = audit_path(self.data_dir, "user-a", "bot")

    def stored(self) -> dict:
        return yaml.safe_load(self.read("user-a/agents/bot.yaml"))

    def audit_events(self) -> list[dict]:
        if not self.audit.exists():
            return []
        lines = [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if line.get("event") == "approval_rule"]


class StoreTests(RulesFixture):
    def test_add_remove_and_clear_go_through_the_agent_file(self) -> None:
        store = self.service._agents
        added = store.add_approval_rules("user-a", "bot", [learned("ls"), learned("pwd")])
        self.assertEqual([rule.command for rule in added], ["ls", "pwd"])
        self.assertEqual([rule["command"] for rule in self.stored()["approval_rules"]["rules"]], ["ls", "pwd"])
        removed = store.remove_approval_rule("user-a", "bot", 0)
        self.assertEqual(removed.command, "ls")
        self.assertEqual([rule.command for rule in store.approval_rules("user-a", "bot")], ["pwd"])
        self.assertEqual([rule.command for rule in store.clear_approval_rules("user-a", "bot")], ["pwd"])
        self.assertNotIn("approval_rules", self.stored())  # back to the defaults: the key is gone

    def test_identical_rules_are_deduped_even_inside_one_call(self) -> None:
        store = self.service._agents
        self.assertEqual(len(store.add_approval_rules("user-a", "bot", [learned("ls"), learned("ls")])), 1)
        self.assertEqual(store.add_approval_rules("user-a", "bot", [learned("ls")]), [])
        self.assertEqual(len(self.stored()["approval_rules"]["rules"]), 1)

    def test_clear_keeps_the_layer_default(self) -> None:
        self.write_agent("bot", AGENT + HAND_WRITTEN)
        self.service._agents.clear_approval_rules("user-a", "bot")
        self.assertEqual(self.stored()["approval_rules"], {"default": "ask", "rules": []})

    def test_an_unknown_position_is_an_error_and_writes_nothing(self) -> None:
        before = self.read("user-a/agents/bot.yaml")
        for index in (0, -1, 5):
            with self.subTest(index=index), self.assertRaises(UnknownApprovalRule):
                self.service._agents.remove_approval_rule("user-a", "bot", index)
        self.assertEqual(self.read("user-a/agents/bot.yaml"), before)

    def test_a_learned_rule_is_revalidated_with_the_guardrail(self) -> None:
        # an agent that already holds a blanket allow on a machine below admin is invalid: nothing may be added to it
        self.write_agent("bot", AGENT.replace("{sandbox: {}}", "{pc: {}}").replace("default_backend: sandbox", "default_backend: pc"))
        before = self.read("user-a/agents/bot.yaml")
        self.service._agents.add_approval_rules("user-a", "bot", [learned("ls", "pc")])  # a narrow rule is fine on a machine
        self.write_agent("bot", before + "approval_rules:\n  rules: [{verdict: allow}]\n")
        broken = self.read("user-a/agents/bot.yaml")
        with self.assertRaises(ApprovalRuleNotAllowed):
            self.service._agents.add_approval_rules("user-a", "bot", [learned("pwd", "pc")])
        self.assertEqual(self.read("user-a/agents/bot.yaml"), broken)

    def test_a_blanket_rule_cannot_be_added_below_admin_on_a_machine(self) -> None:
        self.write_agent("bot", AGENT.replace("{sandbox: {}}", "{pc: {}}").replace("default_backend: sandbox", "default_backend: pc"))
        blanket = ApprovalRule(verdict="allow", tool="terminal", backend="pc")
        with self.assertRaises(ApprovalRuleNotAllowed):
            self.service._agents.add_approval_rules("user-a", "bot", [blanket])
        self.assertNotIn("approval_rules", self.stored())

    def test_saving_the_agent_without_rules_keeps_the_stored_ones(self) -> None:
        self.service._agents.add_approval_rules("user-a", "bot", [learned("ls")])
        body = self.service.get_agent("user-a", "bot")
        body.pop("approval_rules")  # what the web form sends: it does not know the field
        body["name"] = "Renamed"
        from src.config.models import AgentConfig

        self.service.update_agent("user-a", "bot", AgentConfig.model_validate(body))
        self.assertEqual(self.stored()["name"], "Renamed")
        self.assertEqual(self.stored()["approval_rules"]["rules"][0]["command"], "ls")
        body["approval_rules"] = {"rules": []}  # an explicit empty list clears
        self.service.update_agent("user-a", "bot", AgentConfig.model_validate(body))
        self.assertEqual(load_agent_config("user-a", "bot").approval_rules.rules, [])


class AuditTests(RulesFixture):
    def test_removing_and_resetting_write_audit_lines_with_the_actor(self) -> None:
        self.service._agents.add_approval_rules("user-a", "bot", [learned("ls"), learned("pwd"), learned("id")])
        self.service.remove_approval_rule("user-a", "bot", 1)
        self.service.clear_approval_rules("user-a", "bot")
        events = self.audit_events()
        self.assertEqual([(e["action"], e["rule"]["command"]) for e in events], [("remove", "pwd"), ("remove", "ls"), ("remove", "id")])
        self.assertEqual({e["actor"] for e in events}, {"user"})
        self.assertEqual((events[0]["index"], events[1].get("via")), (1, "reset"))
        self.assertEqual({(e["user"], e["agent"]) for e in events}, {("user-a", "bot")})

    def test_audit_lines_do_not_leak_a_secret_in_a_rule(self) -> None:
        rule = ApprovalRule(verdict="allow", command="echo sk-or-abcdefghijklmnop", exact=True, backend="sandbox")
        self.service._agents.add_approval_rules("user-a", "bot", [rule])  # (the card never offers this; a hand edit could)
        self.service.clear_approval_rules("user-a", "bot")
        self.assertNotIn("abcdefghijklmnop", self.audit.read_text(encoding="utf-8"))


class WebApiTests(RulesFixture):
    def setUp(self) -> None:
        super().setUp()
        (self.config_dir / "user-b" / "agents").mkdir(parents=True)
        (self.config_dir / "user-b" / "user.yaml").write_text(
            USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("user-a", "user-b").replace("User A", "User B").replace("USER_A_KEY_NAME", "USER_B_KEY_NAME"),
            encoding="utf-8",
        )
        (self.config_dir / "user-b" / "agents" / "bot.yaml").write_text("id: bot\nname: Other\n", encoding="utf-8")
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-a")
        self.other = TestClient(create_app(self.service))
        self.other.__enter__()
        self.addCleanup(self.other.__exit__, None, None, None)
        login(self.other, "user-b")
        self.url = "/api/agents/bot/approval-rules"

    def fill(self) -> None:
        self.service._agents.add_approval_rules("user-a", "bot", [learned("ls"), learned("pwd")])

    def test_list_shows_index_learned_flag_and_the_rule(self) -> None:
        self.assertEqual(self.client.get(self.url).json(), [])
        self.fill()
        self.write_agent("bot", self.read("user-a/agents/bot.yaml") + "")  # keep as is
        listed = self.client.get(self.url).json()
        self.assertEqual([entry["index"] for entry in listed], [0, 1])
        self.assertTrue(all(entry["learned"] for entry in listed))
        self.assertEqual(listed[0]["rule"], {
            "verdict": "allow", "tool": "terminal", "command": "ls", "backend": "sandbox", "exact": True, "reason": LEARNED_REASON,
        })

    def test_hand_written_rules_are_listed_but_not_marked_learned(self) -> None:
        self.write_agent("bot", AGENT + HAND_WRITTEN)
        listed = self.client.get(self.url).json()
        self.assertEqual((len(listed), listed[0]["learned"], listed[0]["rule"]["id"]), (1, False, "nope"))

    def test_delete_one_by_index_returns_what_remains(self) -> None:
        self.fill()
        response = self.client.delete(f"{self.url}/0")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([entry["rule"]["command"] for entry in response.json()], ["pwd"])
        self.assertEqual([entry["index"] for entry in response.json()], [0])
        self.assertEqual(self.client.delete(f"{self.url}/7").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.url}/-1").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.url}/x").status_code, 422)

    def test_delete_all_and_reset_clear_the_rules(self) -> None:
        self.fill()
        self.assertEqual(self.client.delete(self.url).json(), [])
        self.assertEqual(self.client.get(self.url).json(), [])
        self.fill()
        self.assertEqual(self.client.post(f"{self.url}/reset").json(), [])
        self.assertNotIn("approval_rules", self.stored())
        self.assertEqual(len(self.audit_events()), 4)

    def test_only_the_owner_can_list_or_change_the_rules(self) -> None:
        self.fill()
        self.assertEqual(self.other.get(self.url).json(), [])  # user-b's own `bot` is another agent with no rules
        self.assertEqual(self.other.delete(f"{self.url}/0").status_code, 404)
        self.assertEqual(self.other.delete(self.url).json(), [])
        self.assertEqual(len(self.stored()["approval_rules"]["rules"]), 2)  # user-a's rules are untouched
        self.assertEqual(self.other.get("/api/agents/unknown/approval-rules").status_code, 404)
        anonymous = TestClient(create_app(self.service))
        self.assertEqual(anonymous.get(self.url).status_code, 401)
        self.assertEqual(anonymous.delete(self.url).status_code, 401)

    def test_the_approval_answer_validates_the_scope(self) -> None:
        base = "/api/agents/bot/chats/c1/approvals/abc"
        self.assertEqual(self.client.post(base, json={"decision": "approve", "scope": "forever"}).status_code, 422)
        self.assertEqual(self.client.post(base, json={"decision": "maybe"}).status_code, 422)
        # a valid body for an unknown approval is a 404, scope or not
        self.assertEqual(self.client.post(base, json={"decision": "approve", "scope": "agent"}).status_code, 404)
        self.assertEqual(self.client.post(base, json={"decision": "deny", "scope": "session"}).status_code, 404)
        self.assertEqual(self.client.post(base, json={"decision": "approve"}).status_code, 404)  # scope defaults to once


class CliTests(RulesFixture):
    def test_list_and_clear(self) -> None:
        code, out, err = self.run_cli(["agent", "rules", "--user", "user-a", "--agent", "bot"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("has no approval rules of its own", out)
        self.service._agents.add_approval_rules("user-a", "bot", [learned("git status -s"), learned("ls")])
        code, out, err = self.run_cli(["agent", "rules", "--user", "user-a", "--agent", "bot"])
        self.assertEqual(code, 0, err)
        self.assertIn("[0] allow terminal: git status -s on sandbox  (exact, learned)", out)
        self.assertIn("[1] allow terminal: ls on sandbox", out)
        code, out, err = self.run_cli(["agent", "rules", "--user", "user-a", "--agent", "bot", "--clear"])
        self.assertEqual(code, 0, err)
        self.assertIn("Removed 2 approval rule(s)", out)
        self.assertEqual(self.service.list_approval_rules("user-a", "bot"), [])
        self.assertEqual({e["actor"] for e in self.audit_events()}, {"admin"})
        code, out, _ = self.run_cli(["agent", "rules", "--user", "user-a", "--agent", "bot", "--clear"])
        self.assertIn("Removed 0", out)

    def test_unknown_agent_and_user_fail_readably(self) -> None:
        code, _, err = self.run_cli(["agent", "rules", "--user", "user-a", "--agent", "nope"])
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error:"))
        code, _, err = self.run_cli(["agent", "rules", "--user", "nobody", "--agent", "bot"])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)


if __name__ == "__main__":
    unittest.main()
