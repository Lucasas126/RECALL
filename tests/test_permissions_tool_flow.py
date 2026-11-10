"""The `permissions` tools end to end: reads, cards, applied changes, the audit trail and the failure paths."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from permissions_tool_helpers import BOT_GROUPS, MORE_GROUPS, OPS, PermissionsFixture, Run

from src.config.levels import Level
from src.terminal.audit import audit_path


class Reads(PermissionsFixture):
    def test_agents_list_has_level_required_declared_groups_and_rule_count(self) -> None:
        self.write_agent("ops", OPS + "approval_rules:\n  rules:\n    - {verdict: deny, command: 'rm *'}\n    - {verdict: ask, command: 'git push'}\n")
        self.write_agent("bot", "id: bot\nname: Bot\nchat_config:\n  tool_groups: [memory_read]\n")  # no declared level
        data = json.loads(self.call("agents_list", Run(self.service)))
        by_id = {item["id"]: item for item in data["agents"]}
        self.assertEqual(data["max_level"], "operator")
        self.assertEqual(by_id["bot"], {
            "id": "bot", "name": "Bot", "level": "reader", "required_level": "reader", "declared_level": None,
            "tool_groups": ["memory_read"], "learned_rules": 0, "is_you": False,
        })
        self.assertEqual(by_id["ops"]["learned_rules"], 2)
        self.assertEqual(by_id["ops"]["declared_level"], "operator")
        self.assertTrue(by_id["boss"]["is_you"])

    def test_get_permissions_shows_backends_with_kind_and_the_rules_per_layer(self) -> None:
        self.write_agent("box", "id: box\nname: Box\nlevel: operator\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: sandbox\n"
                         "  backends: {sandbox: {}, pc: {approvals: manual}}\napproval_rules:\n  default: ask\n  rules:\n    - {verdict: deny, command: 'rm *'}\n")
        data = json.loads(self.call("agent_get_permissions", Run(self.service), agent_id="box"))
        self.assertEqual(data["level"], "operator")
        self.assertEqual(data["max_level"], "operator")
        self.assertEqual(data["required_level"], "operator")
        self.assertEqual(data["tool_groups"], ["terminal"])
        kinds = {backend["name"]: backend["kind"] for backend in data["terminal_backends"]}
        self.assertEqual(kinds, {"sandbox": "sandbox", "pc": "machine"})
        self.assertEqual(data["approval_rules"]["agent"], [{"index": 0, "rule": {"verdict": "deny", "tool": "any", "command": "rm *"}}])
        self.assertEqual(data["approval_rules"]["agent_default"], "ask")
        self.assertEqual(set(data["approval_rules"]), {"installation", "user", "agent", "agent_default"})

    def test_a_backend_the_user_does_not_define_is_reported_not_hidden(self) -> None:
        self.write_agent("box", "id: box\nname: Box\nlevel: operator\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  backends: {gone: {}}\n")
        data = json.loads(self.call("agent_get_permissions", Run(self.service), agent_id="box"))
        self.assertEqual(data["terminal_backends"][0]["defined"], False)

    def test_reading_oneself_is_allowed_and_a_broken_agent_does_not_hide_the_others(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: nonsense\n")
        data = json.loads(self.call("agents_list", Run(self.service)))
        self.assertEqual([item["id"] for item in data["agents"]], ["boss", "bot", "ops", "box"])
        self.assertIn("error", data["agents"][1])
        self.assertEqual(json.loads(self.call("agent_get_permissions", Run(self.service), agent_id="boss"))["id"], "boss")

    def test_reads_never_ask_and_never_write(self) -> None:
        before = {name: self.read(f"user-a/agents/{name}.yaml") for name in ("boss", "bot", "ops", "box")}
        run = Run(self.service)
        for name, args in (("agents_list", {}), ("agent_get_permissions", {"agent_id": "bot"}), ("permission_levels_help", {})):
            self.call(name, run, **args)
        self.assertEqual(run.cards, [])
        self.assertEqual(before, {name: self.read(f"user-a/agents/{name}.yaml") for name in before})


class Changes(PermissionsFixture):
    def test_the_card_names_caller_target_and_the_diff(self) -> None:
        run = Run(self.service)
        self.call("agent_set_tool_groups", run, agent_id="bot", groups=MORE_GROUPS)
        card = run.cards[0]
        self.assertEqual(card["tool"], "agent_set_tool_groups")
        self.assertEqual(card["options"], ["once"])
        self.assertEqual((card["agent_id"], card["chat_id"]), ("boss", "chat1"))
        for expected in ("Agent 'Boss' [boss]", "agent 'Bot' [bot]", "before: memory_read, memory_edit", "after:  memory_read, memory_edit, web"):
            self.assertIn(expected, card["command"])
        self.assertIn("always allow", card["reason"])

    def test_groups_card_and_result(self) -> None:
        run = Run(self.service)
        result = json.loads(self.call("agent_set_tool_groups", run, agent_id="bot", groups=["memory_read", "web"]))
        self.assertEqual((result["before"], result["after"]), (["memory_read", "memory_edit"], ["memory_read", "web"]))
        self.assertIn("before: memory_read, memory_edit", run.cards[0]["command"])
        self.assertIn("after:  memory_read, web", run.cards[0]["command"])

    def test_rule_add_remove_and_reset(self) -> None:
        run = Run(self.service, answers=[("approve", "once")] * 4)
        rule = {"verdict": "ask", "tool": "terminal", "command": "git push"}
        self.assertEqual(json.loads(self.call("agent_add_rule", run, agent_id="ops", rule=rule))["after"], {"rules": 1})
        self.call("agent_add_rule", run, agent_id="ops", rule={"verdict": "deny", "tool": "terminal", "command": "rm *"})
        self.assertIn("+ ", run.cards[0]["command"])
        removed = json.loads(self.call("agent_remove_rule", run, agent_id="ops", index=0))
        self.assertEqual((removed["index"], removed["rule"]["command"]), (0, "git push"))
        self.assertIn("less restricted", run.cards[2]["command"])  # an ask rule removed
        reset = json.loads(self.call("agent_reset_rules", run, agent_id="ops"))
        self.assertEqual(reset["after"], {"rules": 0})
        self.assertNotIn("approval_rules", self.stored("ops"))

    def test_reset_keeps_the_layer_default(self) -> None:
        self.write_agent("ops", OPS + "approval_rules:\n  default: ask\n  rules:\n    - {verdict: deny, command: 'rm *'}\n")
        self.call("agent_reset_rules", Run(self.service), agent_id="ops")
        self.assertEqual(self.stored("ops")["approval_rules"], {"default": "ask", "rules": []})

    def test_nothing_to_change_asks_nobody(self) -> None:
        run = Run(self.service)
        for name, args in (
            ("agent_set_tool_groups", {"agent_id": "bot", "groups": BOT_GROUPS}),
            ("agent_set_tool_groups", {"agent_id": "bot", "groups": ["memory_edit", "memory_read"]}),
            ("agent_reset_rules", {"agent_id": "ops"}),
        ):
            with self.subTest(tool=name):
                self.assertEqual(json.loads(self.call(name, run, **args))["status"], "unchanged")
        rule = {"verdict": "deny", "tool": "terminal", "command": "rm *"}
        Run(self.service)
        self.call("agent_add_rule", Run(self.service), agent_id="ops", rule=rule)
        again = Run(self.service)
        self.assertEqual(json.loads(self.call("agent_add_rule", again, agent_id="ops", rule={**rule, "reason": "set by agent boss"}))["status"], "unchanged")
        self.assertEqual((run.cards, again.cards), ([], []))

    def test_the_other_settings_of_the_agent_survive_a_change(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\ndescription: keeps me\nlevel: assistant\nchat_config:\n  tool_groups: [memory_read]\n  reasoning: false\nfolders: []\n")
        self.call("agent_set_tool_groups", Run(self.service), agent_id="bot", groups=MORE_GROUPS)
        stored = self.stored("bot")
        self.assertEqual((stored["description"], stored["chat_config"]["reasoning"], stored["level"]), ("keeps me", False, "assistant"))

    def test_the_next_reply_of_the_target_sees_the_change(self) -> None:
        before = self.service._agents.fingerprint("user-a", "bot")
        self.call("agent_set_tool_groups", Run(self.service), agent_id="bot", groups=MORE_GROUPS)
        self.assertNotEqual(self.service._agents.fingerprint("user-a", "bot"), before)  # the service rebuilds on a changed file

    def test_the_target_must_be_valid_after_the_change(self) -> None:
        # lowering the level of an undeclared agent is a declaration; a level that the store refuses is reported, nothing written
        before = self.read("user-a/agents/ops.yaml")
        with patch("src.config.agents.check_agent_level", side_effect=ValueError("boom")):
            result = self.call("agent_set_tool_groups", Run(self.service), agent_id="ops", groups=MORE_GROUPS)
        self.assertIn("error:", result)
        self.assertEqual(self.read("user-a/agents/ops.yaml"), before)


class Audit(PermissionsFixture):
    def test_an_approved_change_is_audited_for_target_and_caller(self) -> None:
        run = Run(self.service)
        self.call("agent_set_tool_groups", run, agent_id="bot", groups=MORE_GROUPS)
        for owner in ("bot", "boss"):
            (line,) = self.audit_events(owner)
            with self.subTest(file=owner):
                self.assertEqual(
                    {key: line[key] for key in ("event", "actor", "actor_kind", "user", "target", "tool", "field", "before", "after", "decision", "applied")},
                    {"event": "permission_change", "actor": "boss", "actor_kind": "agent", "user": "user-a", "target": "bot",
                     "tool": "agent_set_tool_groups", "field": "tool_groups", "before": ["memory_read", "memory_edit"], "after": ["memory_read", "memory_edit", "web"], "decision": "approved", "applied": True},
                )
                self.assertEqual(line["approval_id"], run.cards[0]["id"])
                self.assertEqual(line["chat"], "chat1")
        self.assertEqual(self.audit_events("bot")[0]["agent"], "bot")
        self.assertEqual(self.audit_events("boss")[0]["agent"], "boss")

    def test_a_denied_change_is_audited_and_not_applied(self) -> None:
        self.call("agent_set_tool_groups", Run(self.service, answers=[("deny", "once")]), agent_id="bot", groups=MORE_GROUPS)
        (line,) = self.audit_events("bot")
        self.assertEqual((line["decision"], line["reason"], line["after"]), ("denied", "denied by user", MORE_GROUPS))
        self.assertNotIn("applied", line)

    def test_a_timeout_is_audited_as_denied(self) -> None:
        self.service._approvals.timeout_seconds = 0.3
        self.call("agent_set_tool_groups", Run(self.service, silent=True), agent_id="bot", groups=MORE_GROUPS)
        self.assertEqual(self.audit_events("bot")[0]["reason"], "timed out")

    def test_a_refused_attempt_is_audited_in_the_callers_file_only(self) -> None:
        run = Run(self.service)
        self.call("agent_set_tool_groups", run, agent_id="bot", groups=["memory_read", "terminal"])
        self.assertEqual(self.audit_events("bot"), [])
        (line,) = self.audit_events("boss")
        self.assertEqual((line["decision"], line["target"], line["field"]), ("refused", "bot", "tool_groups"))
        self.assertIn("not allowed at the level", line["reason"])

    def test_an_id_that_is_no_agent_never_creates_a_folder(self) -> None:
        self.call("agent_set_tool_groups", Run(self.service), agent_id="stranger", groups=["memory_read"])
        self.assertFalse(audit_path(self.data_dir, "user-a", "stranger").parent.exists())
        self.assertFalse(audit_path(self.data_dir, "user-b", "stranger").parent.exists())

    def test_rule_commands_are_redacted_in_the_audit(self) -> None:
        run = Run(self.service)
        self.call("agent_add_rule", run, agent_id="ops", rule={"verdict": "deny", "tool": "terminal", "command": "curl -H 'Authorization: Bearer abcdef0123456789abcdef' x"})
        (line,) = self.audit_events("ops")
        self.assertNotIn("abcdef0123456789abcdef", json.dumps(line))

    def test_without_a_writable_audit_trail_nothing_is_asked_or_changed(self) -> None:
        run = Run(self.service)
        with patch("src.service.agent_permissions.audit_writable", return_value=False):
            result = self.call("agent_set_tool_groups", run, agent_id="bot", groups=MORE_GROUPS)
        self.assertIn("audit trail", result)
        self.assertEqual(run.cards, [])
        self.assertEqual(self.stored("bot")["chat_config"]["tool_groups"], BOT_GROUPS)


class Races(PermissionsFixture):
    def test_a_change_made_while_the_card_is_open_is_not_applied(self) -> None:
        run = Run(self.service)
        emit = run.emit

        def tamper(kind, data):
            if kind == "approval":  # the user (or another process) edits the agent while the card is open
                self.write_agent("bot", "id: bot\nname: Bot\nlevel: assistant\nchat_config:\n  tool_groups: [memory_read]\n")
            emit(kind, data)

        run.emit = tamper  # type: ignore[method-assign]
        result = self.call("agent_set_tool_groups", run, agent_id="bot", groups=MORE_GROUPS)
        self.assertIn("changed while the approval was pending", result)
        self.assertEqual(self.stored("bot")["chat_config"]["tool_groups"], ["memory_read"])
        (line,) = self.audit_events("bot")
        self.assertEqual((line["decision"], line["applied"]), ("approved", False))

    def test_the_user_lowering_the_max_level_while_the_card_is_open_blocks_the_change(self) -> None:
        run = Run(self.service)
        emit = run.emit

        def lower(kind, data):
            if kind == "approval":
                self.write_user(self.read("user-a/user.yaml").replace("max_level: operator", "max_level: reader"))
            emit(kind, data)

        run.emit = lower  # type: ignore[method-assign]
        result = self.call("agent_set_tool_groups", run, agent_id="bot", groups=MORE_GROUPS)
        self.assertIn("error:", result)
        self.assertEqual(self.stored("bot")["chat_config"]["tool_groups"], BOT_GROUPS)


class ServicePath(PermissionsFixture):
    def test_the_web_answer_path_refuses_wider_scopes_for_the_card(self) -> None:
        run = Run(self.service, silent=True)
        self.service._approvals.timeout_seconds = 5
        import threading

        outcome = {}

        def asker():
            outcome["result"] = self.call("agent_set_tool_groups", run, agent_id="bot", groups=MORE_GROUPS)

        thread = threading.Thread(target=asker)
        thread.start()
        for _ in range(100):
            if run.cards:
                break
            threading.Event().wait(0.05)
        approval_id = run.cards[0]["id"]
        from src.utils.i18n import InputError

        for scope in ("session", "agent"):
            with self.assertRaises(InputError):
                self.service.resolve_approval("user-a", "boss", "chat1", approval_id, "approve", scope)
        self.assertEqual(self.service.resolve_approval("user-a", "boss", "chat1", approval_id, "approve", "once"), {"status": "approved", "scope": "once"})
        thread.join(5)
        self.assertIn("applied", outcome["result"])

    def test_the_service_builds_the_port_and_hands_it_to_each_run(self) -> None:
        from src.service.agent_permissions import AgentPermissions

        self.assertIsInstance(self.service._permissions, AgentPermissions)


class LevelHelp(PermissionsFixture):
    def test_help_is_read_only_text_for_every_level_and_capability(self) -> None:
        text = self.call("permission_levels_help", Run(self.service))
        for level in Level:
            self.assertIn(f"{level.label} ({int(level)})", text)
        answer = self.call("permission_levels_help", Run(self.service), capability="terminal")
        self.assertIn("operator", answer)
        self.assertIn("error: unknown capability", self.call("permission_levels_help", Run(self.service), capability="fly"))


if __name__ == "__main__":
    unittest.main()
