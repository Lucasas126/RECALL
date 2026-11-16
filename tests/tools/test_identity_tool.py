"""The `identity` group: `set_display_name` changes only the caller's own `name`, audited, never in a disposable chat."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

import yaml
from tests.support.agents_tool_helpers import AgentsFixture
from tests.support.permissions_tool_helpers import resolved

from src.agent.tool_groups import DEFAULT_TOOL_GROUPS, DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_CATALOG, TOOL_GROUP_TOOLS, validate_tool_groups
from src.agent.toolsets.identity import build_identity_tools
from src.agent.tools import build_tools
from src.config.levels import Level, selected_tool_groups
from src.config.loader import load_agent_config
from src.service.agent_admin import NAME_MAX
from src.terminal.context import run_context

NANA = "id: nana\nname: Nana\nlevel: assistant\ndescription: keep me\nchat_config:\n  tool_groups: [memory_read, identity]\n"


class IdentityTool(AgentsFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_user(self.read("user-a/user.yaml").replace("[chief,", "[nana, chief,"))
        self.write_agent("nana", NANA)

    def rename(self, name, agent="nana", level=Level.ASSISTANT, groups=("memory_read", "identity")) -> str:
        func = {f.tool_spec["function"]["name"]: f for f in build_identity_tools(resolved("user-a", agent, level, list(groups)))}
        with run_context(self.run_for(agent).context()):
            return func["set_display_name"](name=name)

    def run_for(self, agent: str):
        from tests.support.permissions_tool_helpers import Run
        from tests.support.agents_tool_helpers import AgentsRun
        return AgentsRun(self.service, agent, ())

    def stored(self, agent: str = "nana") -> dict:
        return yaml.safe_load(self.read(f"user-a/agents/{agent}.yaml"))

    def test_valid_rename_changes_only_the_name(self) -> None:
        before = load_agent_config("user-a", "nana").model_dump(mode="json")
        others = {k: v for k, v in self.files_of("user-a").items() if k != "nana.yaml"}
        out = self.rename("  Luna  ")
        self.assertTrue(out.startswith("done:"), out)
        after = load_agent_config("user-a", "nana").model_dump(mode="json")
        self.assertEqual(after["name"], "Luna")
        self.assertEqual({k: v for k, v in after.items() if k != "name"}, {k: v for k, v in before.items() if k != "name"})
        self.assertEqual(others, {k: v for k, v in self.files_of("user-a").items() if k != "nana.yaml"})

    def test_invalid_names_are_refused_and_change_nothing(self) -> None:
        text = self.read("user-a/agents/nana.yaml")
        for bad in ("", "   ", "a\nb", "a\tb", "a\x00b", "a\x1bb", "x" * (NAME_MAX + 1), 5):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertTrue(self.rename(bad).startswith("error:"))
        self.assertEqual(self.read("user-a/agents/nana.yaml"), text)
        self.assertTrue(self.rename("x" * NAME_MAX).startswith("done:"))

    def test_same_name_is_unchanged(self) -> None:
        self.assertTrue(self.rename("Nana").startswith("unchanged:"))
        self.assertEqual(self.audit_lines("nana"), [])

    def test_it_only_acts_on_the_caller(self) -> None:
        func = build_identity_tools(resolved("user-a", "nana", Level.ASSISTANT, ["identity"]))[0]
        self.assertEqual(list(func.tool_spec["function"]["parameters"]["properties"]), ["name"])
        with run_context(self.run_for("chief").context()):  # the run belongs to another agent
            self.assertTrue(func(name="Hijack").startswith("error:"))
        self.assertEqual(self.stored()["name"], "Nana")
        self.assertEqual(self.stored("chief")["name"], "Chief")

    def test_refused_without_a_chat_run_or_port(self) -> None:
        func = build_identity_tools(resolved("user-a", "nana", Level.ASSISTANT, ["identity"]))[0]
        self.assertTrue(func(name="Luna").startswith("error:"))
        run = self.run_for("nana")
        run.with_port = False
        with run_context(run.context()):
            self.assertTrue(func(name="Luna").startswith("error:"))
        self.assertEqual(self.stored()["name"], "Nana")

    def test_refused_when_the_stored_agent_lost_the_group(self) -> None:
        self.write_agent("nana", NANA.replace(", identity", ""))
        self.assertTrue(self.rename("Luna").startswith("error:"))
        self.assertEqual(self.stored()["name"], "Nana")

    def test_the_audit_records_old_and_new_name(self) -> None:
        self.rename("Luna")
        (line,) = [l for l in self.audit_lines("nana") if l["event"] == "permission_change"]
        self.assertEqual(
            (line["tool"], line["field"], line["before"], line["after"], line["decision"], line["actor"], line["target"]),
            ("set_display_name", "name", "Nana", "Luna", "applied", "nana", "nana"),
        )

    def test_the_running_service_sees_the_new_name(self) -> None:
        self.rename("Luna")
        names = {a["id"]: a["name"] for a in self.service.list_agents("user-a")}
        self.assertEqual(names["nana"], "Luna")
        self.assertEqual(load_agent_config("user-a", "nana").name, "Luna")


class IdentityGroup(unittest.TestCase):
    def agent(self, level: Level = Level.ASSISTANT):
        return SimpleNamespace(sources=Mock(), config=resolved("user-a", "nana", level, []))

    @staticmethod
    def names(specs) -> set[str]:
        return {spec["function"]["name"] for spec in specs}

    def test_catalog_defaults_and_validation(self) -> None:
        self.assertIn("identity", {item["id"] for item in TOOL_GROUP_CATALOG})
        self.assertIn("identity", DEFAULT_TOOL_GROUPS)
        self.assertEqual(TOOL_GROUP_TOOLS["identity"], ("set_display_name",))
        self.assertEqual(validate_tool_groups(["identity"]), ["identity"])
        self.assertLessEqual(set(TOOL_GROUP_TOOLS["identity"]), DISPOSABLE_BLOCKED_TOOLS)

    def test_present_by_default_absent_when_not_selected(self) -> None:
        agent = self.agent()
        self.assertIn("set_display_name", self.names(build_tools(agent, None, list(DEFAULT_TOOL_GROUPS))[0]))
        self.assertNotIn("set_display_name", self.names(build_tools(agent, None, ["memory_read", "self"])[0]))

    def test_an_agent_without_tool_groups_gets_it_and_an_explicit_list_does_not(self) -> None:
        cfg = SimpleNamespace(tools=True, tool_groups=None)
        self.assertIn("identity", selected_tool_groups(SimpleNamespace(chat_config=SimpleNamespace(tools=True, tool_groups=None))))
        self.assertNotIn("identity", selected_tool_groups(SimpleNamespace(chat_config=SimpleNamespace(tools=True, tool_groups=["web"]))))
        del cfg

    def test_absent_in_disposable_chats_and_automations(self) -> None:
        agent = self.agent()
        for extra in ({"disposable": True}, {"disposable": True, "automation": True}):
            specs, handlers = build_tools(agent, None, ["identity"], **extra)
            self.assertNotIn("set_display_name", self.names(specs) | set(handlers))

    def test_level_gate(self) -> None:
        self.assertIn("set_display_name", self.names(build_tools(self.agent(Level.READER), None, ["identity"])[0]))
        self.assertNotIn("set_display_name", self.names(build_tools(self.agent(Level.MINIMAL), None, ["identity"])[0]))


if __name__ == "__main__":
    unittest.main()
