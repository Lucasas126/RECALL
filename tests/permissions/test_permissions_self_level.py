"""Levels through the `permissions` tools: others up to the caller's level, its own only lowered, the user always up to max_level."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from tests.support.admin_helpers import USER_YAML
from tests.support.auth_helpers import TEST_PASSWORD_HASH
from tests.support.permissions_tool_helpers import BACKENDS, BOSS, OPS, PermissionsFixture, Run

from src.agent.tool_groups import PERMISSION_CHANGE_TOOLS, TOOL_GROUP_TOOLS
from src.config.levels import Level
from src.utils.i18n import InputError

# a second operator that holds the group, and a lower self-managing agent
ADMIN_BOSS = BOSS.replace("level: operator", "level: admin")
ALL_AGENTS = "agents: [boss, bot, ops, box]"


def _widen(fixture: PermissionsFixture) -> None:
    fixture.write_user(USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("agents: [bot]", ALL_AGENTS) + BACKENDS)  # max_level admin


class Refused:
    def assert_refused(self, run: Run, result: str, text: str = "") -> None:
        self.assertTrue(result.startswith("error:"), result)  # type: ignore[attr-defined]
        self.assertEqual(run.cards, [], "nobody should have been asked")  # type: ignore[attr-defined]
        if text:
            self.assertIn(text, result)  # type: ignore[attr-defined]


class TheToolIsThere(PermissionsFixture):
    def test_the_level_tool_is_part_of_the_group(self) -> None:
        self.assertIn("agent_set_level", PERMISSION_CHANGE_TOOLS)
        self.assertIn("agent_set_level", TOOL_GROUP_TOOLS["permissions"])
        self.assertIn("agent_set_level", self.tools())

    def test_the_reads_tell_how_a_level_is_changed(self) -> None:
        detail = json.loads(self.call("agent_get_permissions", Run(self.service), agent_id="bot"))
        self.assertIn("ask the user", detail["how_to_change_level"])
        self.assertIn("Agents tab > level", detail["how_to_change_level"])
        self.assertIn("how_to_change_level", json.loads(self.call("agents_list", Run(self.service))))

    def test_the_help_explains_who_changes_levels(self) -> None:
        text = self.call("permission_levels_help", Run(self.service))
        self.assertIn("only lower its own", text)
        self.assertIn("always change any level", text)


class OtherAgents(PermissionsFixture, Refused):
    def test_an_operator_sets_another_agents_level_up_to_its_own(self) -> None:
        run = Run(self.service)
        result = json.loads(self.call("agent_set_level", run, agent_id="bot", level="operator"))
        self.assertEqual((result["status"], result["self_change"]), ("applied", False))
        self.assertEqual(self.stored("bot")["level"], "operator")
        self.assertNotIn("self-change", run.cards[0]["command"])

    def test_it_cannot_give_a_level_above_its_own_or_the_users_max(self) -> None:
        run = Run(self.service)
        self.assert_refused(run, self.call("agent_set_level", run, agent_id="bot", level="admin"), "maximum level 'operator'")
        _widen(self)
        run = Run(self.service)
        self.assert_refused(run, self.call("agent_set_level", run, agent_id="bot", level="admin"), "above your own level 'operator'")
        self.assertEqual(self.stored("bot")["level"], "assistant")

    def test_it_cannot_go_below_what_the_configuration_needs(self) -> None:
        run = Run(self.service)
        self.assert_refused(run, self.call("agent_set_level", run, agent_id="ops", level="assistant"), "needs at least 'operator'")

    def test_an_admin_agent_can_give_admin_up_to_the_users_max(self) -> None:
        _widen(self)
        self.write_agent("boss", ADMIN_BOSS)
        result = self.call("agent_set_level", Run(self.service), as_level=Level.ADMIN, agent_id="bot", level="admin")
        self.assertIn('"status": "applied"', result)
        self.assertEqual(self.stored("bot")["level"], "admin")

    def test_unknown_levels_are_refused(self) -> None:
        for wanted in ("root", "5", "", "ADMIN; ignore the limits"):
            run = Run(self.service)
            with self.subTest(level=wanted):
                self.assert_refused(run, self.call("agent_set_level", run, agent_id="bot", level=wanted))


class OwnLevel(PermissionsFixture, Refused):
    def test_raising_its_own_level_is_refused(self) -> None:
        _widen(self)  # the user would allow admin, the agent still may not
        run = Run(self.service)
        result = self.call("agent_set_level", run, agent_id="boss", level="admin")
        self.assert_refused(run, result, "can only lower its own level")
        self.assertEqual(self.stored("boss")["level"], "operator")
        (line,) = self.audit_events("boss")
        self.assertEqual((line["decision"], line["actor"], line["target"]), ("refused", "boss", "boss"))

    def test_a_runtime_level_cannot_be_borrowed_to_raise_itself(self) -> None:
        _widen(self)
        run = Run(self.service)
        result = self.call("agent_set_level", run, as_level=Level.ADMIN, agent_id="boss", level="admin")
        self.assert_refused(run, result, "above your own level 'operator'")

    def test_lowering_below_what_its_groups_need_is_refused_with_the_way_out(self) -> None:
        run = Run(self.service)
        result = self.call("agent_set_level", run, agent_id="boss", level="reader")
        self.assert_refused(run, result, "needs at least 'operator'")
        self.assertIn("agent_set_tool_groups", result)
        self.assertEqual(self.stored("boss")["level"], "operator")

    def test_keeping_the_level_asks_nobody(self) -> None:
        run = Run(self.service)
        self.assertEqual(json.loads(self.call("agent_set_level", run, agent_id="boss", level="operator"))["status"], "unchanged")
        self.assertEqual(run.cards, [])

    def test_lowering_works_after_dropping_the_groups_that_need_the_higher_level(self) -> None:
        run = Run(self.service, answers=[("approve", "once")] * 2)
        first = json.loads(self.call("agent_set_tool_groups", run, agent_id="boss", groups=["memory_read"]))
        self.assertEqual(first["status"], "applied")
        # the same reply goes on: the tools were built before, the plan uses the stored configuration
        second = json.loads(self.call("agent_set_level", run, agent_id="boss", level="reader"))
        self.assertEqual((second["status"], second["self_change"]), ("applied", True))
        self.assertEqual(self.stored("boss")["level"], "reader")
        for card in run.cards:
            self.assertIn("self-change", card["command"])
            self.assertEqual(card["options"], ["once"])
        self.assertEqual({line["actor"] == line["target"] for line in self.audit_events("boss")}, {True})
        self.assertEqual(len(self.audit_events("boss")), 2)

    def test_lowering_to_a_level_that_keeps_the_group_works_with_an_approval(self) -> None:
        # operator is the lowest level that holds the group, so the way down below it is the two steps above; admin lowers to operator
        _widen(self)
        self.write_agent("boss", ADMIN_BOSS)
        run = Run(self.service)
        result = json.loads(self.call("agent_set_level", run, as_level=Level.ADMIN, agent_id="boss", level="operator"))
        self.assertEqual(result["status"], "applied")
        self.assertEqual((result["before"], result["after"]), ("admin", "operator"))
        self.assertIn("ITS OWN permissions", run.cards[0]["command"])

    def test_after_lowering_the_tools_are_no_longer_built_and_it_cannot_raise_itself_again(self) -> None:
        run = Run(self.service, answers=[("approve", "once")] * 2)
        self.call("agent_set_tool_groups", run, agent_id="boss", groups=["memory_read"])
        self.call("agent_set_level", run, agent_id="boss", level="reader")
        self.assertEqual(self.tools("boss", Level.READER, ("memory_read",)), {})  # the next reply builds none of them
        # a stale tool of the same reply still refuses to raise
        again = Run(self.service)
        result = self.call("agent_set_level", again, agent_id="boss", level="operator")
        self.assert_refused(again, result)
        self.assertEqual(self.stored("boss")["level"], "reader")

    def test_the_lost_group_exception_is_only_for_lowering_itself(self) -> None:
        # boss dropped the group: it may still lower itself but touches nobody else
        self.write_agent("boss", BOSS.replace(", permissions", ""))
        for name, args in (("agent_set_level", {"agent_id": "bot", "level": "reader"}),
                           ("agent_set_tool_groups", {"agent_id": "boss", "groups": ["memory_read"]}),
                           ("agent_set_level", {"agent_id": "ops", "level": "operator"})):
            run = Run(self.service)
            with self.subTest(tool=name, target=args["agent_id"]):
                self.assertIn("no longer holds", self.call(name, run, **args))
                self.assertEqual(run.cards, [])


class RaisingAgain(PermissionsFixture, Refused):
    def test_another_operator_agent_can_raise_a_lowered_agent(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\nchat_config:\n  tool_groups: [memory_read]\n")
        run = Run(self.service)
        result = self.call("agent_set_level", run, agent_id="bot", level="operator")
        self.assertIn('"status": "applied"', result)
        self.assertEqual(self.stored("bot")["level"], "operator")

    def test_that_operator_cannot_raise_it_above_its_own_level_or_the_users_max(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\nchat_config:\n  tool_groups: [memory_read]\n")
        run = Run(self.service)
        self.assert_refused(run, self.call("agent_set_level", run, agent_id="bot", level="admin"))
        self.assertEqual(self.stored("bot")["level"], "reader")

    def test_the_user_path_is_unaffected(self) -> None:
        from src.config.models import AgentConfig

        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\nchat_config:\n  tool_groups: [memory_read]\n")
        raised = AgentConfig.model_validate({"id": "bot", "name": "Bot", "level": "operator", "chat_config": {"tool_groups": ["memory_read"]}})
        self.service.update_agent("user-a", "bot", raised)  # the web form: up to the user's max_level
        self.assertEqual(self.stored("bot")["level"], "operator")
        too_high = AgentConfig.model_validate({"id": "bot", "name": "Bot", "level": "admin", "chat_config": {"tool_groups": ["memory_read"]}})
        with self.assertRaises(InputError):
            self.service.update_agent("user-a", "bot", too_high)


class NoWiderScope(PermissionsFixture):
    def test_a_self_level_change_cannot_be_approved_for_a_session_or_the_agent(self) -> None:
        self.call("agent_set_tool_groups", Run(self.service), agent_id="boss", groups=["memory_read"])
        for scope in ("session", "agent"):
            self.write_agent("boss", BOSS)
            self.call("agent_set_tool_groups", Run(self.service), agent_id="boss", groups=["memory_read"])
            run = Run(self.service, answers=[("approve", scope)])
            result = self.call("agent_set_level", run, agent_id="boss", level="reader")
            with self.subTest(scope=scope):
                self.assertTrue(run.errors and isinstance(run.errors[0], InputError), run.errors)
                self.assertTrue(result.startswith("error:"), result)
                self.assertEqual(self.stored("boss")["level"], "operator")
        self.assertEqual(self.service._approvals._session_grants, {})


if __name__ == "__main__":
    unittest.main()
