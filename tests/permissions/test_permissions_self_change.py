"""The `permissions` tools after the self-change rule: an agent may change its own groups and rules inside its level (levels: see test_permissions_self_level.py)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from tests.support.admin_helpers import USER_YAML
from tests.support.auth_helpers import TEST_PASSWORD_HASH
from tests.support.permissions_tool_helpers import BACKENDS, BOSS, MORE_GROUPS, OPS, SANDBOXED, PermissionsFixture, Run

from src.agent.tool_groups import TOOL_GROUP_TOOLS
from src.config import ceiling
from src.config.levels import Level
from src.utils.i18n import InputError

ADMIN_OPS = OPS.replace("level: operator", "level: admin").replace("pc: {}", "local: {}").replace("default_backend: pc", "default_backend: local")


class Unchanged:
    """Asserts a call refused with `error:` before any card."""

    def assert_refused(self, run: Run, result: str, text: str = "") -> None:
        self.assertTrue(result.startswith("error:"), result)  # type: ignore[attr-defined]
        self.assertEqual(run.cards, [], "nobody should have been asked")  # type: ignore[attr-defined]
        if text:
            self.assertIn(text, result)  # type: ignore[attr-defined]


class SelfChange(PermissionsFixture, Unchanged):
    """An agent may change its own groups and rules, inside its own effective level, with the user's approval."""

    NARROW = {"verdict": "deny", "tool": "terminal", "command": "rm *"}

    def self_ops(self, name: str, run: Run, **args) -> str:
        """ops holds `permissions` too (operator, a real machine) and calls on itself."""
        self.write_agent("ops", OPS.replace("tool_groups: [terminal]", "tool_groups: [terminal, permissions]"))
        return self.call(name, run, as_agent="ops", agent_id="ops", **args)

    def test_adding_a_narrow_rule_to_itself_works_with_an_approval(self) -> None:
        run = Run(self.service, agent_id="ops")
        result = json.loads(self.self_ops("agent_add_rule", run, rule=self.NARROW))
        self.assertEqual((result["status"], result["target"], result["self_change"]), ("applied", "ops", True))
        self.assertEqual(len(run.cards), 1)
        self.assertEqual(run.cards[0]["options"], ["once"])
        self.assertEqual(self.stored("ops")["approval_rules"]["rules"][0]["command"], "rm *")
        self.assertEqual(self.stored("ops")["level"], "operator")

    def test_the_card_says_it_is_a_self_change(self) -> None:
        run = Run(self.service, agent_id="ops")
        self.self_ops("agent_add_rule", run, rule=self.NARROW)
        card = run.cards[0]["command"]
        self.assertIn("ITS OWN permissions", card)
        self.assertIn("self-change", card)
        self.assertIn("Agent 'Ops' [ops]", card)
        self.assertNotIn("asks to change the permissions of agent", card)

    def test_another_agents_card_does_not_say_self_change(self) -> None:
        run = Run(self.service)
        self.call("agent_add_rule", run, agent_id="ops", rule=self.NARROW)
        self.assertNotIn("self-change", run.cards[0]["command"])

    def test_a_self_change_without_approval_changes_nothing(self) -> None:
        run = Run(self.service, agent_id="ops", answers=[("deny", "once")])
        result = self.self_ops("agent_add_rule", run, rule=self.NARROW)
        self.assertIn("did not approve", result)
        self.assertNotIn("approval_rules", self.stored("ops"))

    def test_the_audit_has_one_line_with_actor_equal_to_target(self) -> None:
        run = Run(self.service, agent_id="ops")
        self.self_ops("agent_add_rule", run, rule=self.NARROW)
        (line,) = self.audit_events("ops")
        self.assertEqual((line["actor"], line["target"], line["agent"]), ("ops", "ops", "ops"))
        self.assertEqual((line["decision"], line["applied"], line["tool"]), ("approved", True, "agent_add_rule"))

    def test_a_blanket_allow_on_a_machine_backend_is_refused_below_admin(self) -> None:
        run = Run(self.service, agent_id="ops")
        result = self.self_ops("agent_add_rule", run, rule={"verdict": "allow", "tool": "terminal"})
        self.assert_refused(run, result, text="needs the level 'admin'")
        self.assertNotIn("approval_rules", self.stored("ops"))
        (line,) = self.audit_events("ops")
        self.assertEqual((line["decision"], line["actor"], line["target"]), ("refused", "ops", "ops"))

    def test_a_blanket_allow_on_a_sandbox_is_fine_for_an_operator_itself(self) -> None:
        self.write_agent("box", SANDBOXED.replace("tool_groups: [terminal]", "tool_groups: [terminal, permissions]"))
        run = Run(self.service, agent_id="box")
        result = self.call("agent_add_rule", run, as_agent="box", agent_id="box", rule={"verdict": "allow", "tool": "terminal", "backend": "sandbox"})
        self.assertIn('"status": "applied"', result)

    def test_an_admin_agent_may_blanket_allow_itself_on_local(self) -> None:
        self.write_user(USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("agents: [bot]", "agents: [boss, bot, ops, box]") + BACKENDS)
        self.write_agent("ops", ADMIN_OPS.replace("tool_groups: [terminal]", "tool_groups: [terminal, permissions]"))
        run = Run(self.service, agent_id="ops")
        result = self.call("agent_add_rule", run, as_agent="ops", as_level=Level.ADMIN, agent_id="ops", rule={"verdict": "allow", "tool": "terminal"})
        self.assertIn('"status": "applied"', result)

    def test_adding_a_group_above_its_level_is_refused(self) -> None:
        with patch.dict(ceiling.GROUP_MIN_LEVEL, {"folders_edit": Level.ADMIN}):
            run = Run(self.service)
            result = self.call("agent_set_tool_groups", run, agent_id="boss", groups=["memory_read", "permissions", "folders_edit"])
        self.assert_refused(run, result, text="not allowed at the level 'operator'")
        self.assertEqual(self.stored("boss")["chat_config"]["tool_groups"], ["memory_read", "permissions"])

    def test_adding_a_group_its_level_allows_works(self) -> None:
        run = Run(self.service)
        result = self.call("agent_set_tool_groups", run, agent_id="boss", groups=["memory_read", "permissions", "web"])
        self.assertIn('"status": "applied"', result)
        self.assertEqual(self.stored("boss")["chat_config"]["tool_groups"], ["memory_read", "permissions", "web"])
        self.assertIn("self-change", run.cards[0]["command"])
        self.assertEqual(self.stored("boss")["level"], "operator")

    def test_an_undeclared_agent_is_not_raised_through_its_own_groups(self) -> None:
        # without a declared level the level follows the groups: a group that needs more would raise it silently
        self.write_agent("boss", "id: boss\nname: Boss\nchat_config:\n  tool_groups: [memory_read, permissions]\n")
        run = Run(self.service)
        with patch.dict(ceiling.GROUP_MIN_LEVEL, {"folders_edit": Level.ADMIN}):
            result = self.call("agent_set_tool_groups", run, agent_id="boss", groups=["memory_read", "permissions", "folders_edit"])
        self.assertTrue(result.startswith("error:"), result)
        self.assertEqual(run.cards, [])

    def test_a_runtime_level_below_the_stored_one_caps_the_self_change(self) -> None:
        # stored admin, running as operator: a group only an admin may hold cannot be added by the caller to itself
        self.write_user(USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("agents: [bot]", "agents: [boss, bot, ops, box]") + BACKENDS)
        self.write_agent("boss", BOSS.replace("level: operator", "level: admin"))
        with patch.dict(ceiling.GROUP_MIN_LEVEL, {"folders_edit": Level.ADMIN}):
            run = Run(self.service)
            result = self.call("agent_set_tool_groups", run, agent_id="boss", groups=["memory_read", "permissions", "folders_edit"])
        self.assert_refused(run, result, text="your own level 'operator'")

    def test_removing_and_resetting_its_own_rules_works(self) -> None:
        self.write_agent("ops", OPS.replace("tool_groups: [terminal]", "tool_groups: [terminal, permissions]") + "approval_rules:\n  rules:\n    - {verdict: deny, command: 'rm *'}\n    - {verdict: ask, command: 'git push'}\n")
        run = Run(self.service, agent_id="ops", answers=[("approve", "once")] * 2)
        removed = json.loads(self.call("agent_remove_rule", run, as_agent="ops", agent_id="ops", index=0))
        self.assertEqual(removed["status"], "applied")
        reset = json.loads(self.call("agent_reset_rules", run, as_agent="ops", agent_id="ops"))
        self.assertEqual(reset["after"], {"rules": 0})
        self.assertNotIn("approval_rules", self.stored("ops"))
        self.assertTrue(all("self-change" in card["command"] for card in run.cards))

    def test_a_self_change_cannot_take_what_the_level_needs_away_from_the_level(self) -> None:
        # ops declares operator; dropping terminal is fine, but a change can never need more than the level it has
        self.write_agent("ops", OPS.replace("level: operator", "level: reader").replace("tool_groups: [terminal]", "tool_groups: [memory_read]").replace("terminal:\n  default_backend: pc\n  backends: {pc: {}}\n", ""))
        run = Run(self.service, agent_id="ops")
        result = self.call("agent_set_tool_groups", run, as_agent="ops", agent_id="ops", groups=["memory_read", "terminal"])
        self.assertTrue(result.startswith("error:"), result)

    def test_sessions_and_agent_scopes_are_still_rejected_for_a_self_change(self) -> None:
        for scope in ("session", "agent"):
            self.write_agent("ops", OPS.replace("tool_groups: [terminal]", "tool_groups: [terminal, permissions]"))
            run = Run(self.service, agent_id="ops", answers=[("approve", scope)])
            result = self.call("agent_add_rule", run, as_agent="ops", agent_id="ops", rule=self.NARROW)
            with self.subTest(scope=scope):
                self.assertTrue(run.errors and isinstance(run.errors[0], InputError), run.errors)
                self.assertTrue(result.startswith("error:"), result)
                self.assertNotIn("approval_rules", self.stored("ops"))
        self.assertEqual(self.service._approvals._session_grants, {})

    def test_the_service_refuses_a_level_raising_self_change_directly(self) -> None:
        from src.service.agent_permissions import PermissionRefused

        # an undeclared operator-by-groups agent (terminal) cannot add a group that needs admin
        self.write_agent("ops", "id: ops\nname: Ops\nchat_config:\n  tool_groups: [terminal, permissions]\nterminal:\n  default_backend: pc\n  backends: {pc: {}}\n")
        with patch.dict(ceiling.GROUP_MIN_LEVEL, {"folders_edit": Level.ADMIN}):
            with self.assertRaises(PermissionRefused):
                self.service._permissions.plan_set_tool_groups("user-a", "ops", Level.OPERATOR, "ops", ["terminal", "permissions", "folders_edit"])


if __name__ == "__main__":
    unittest.main()
