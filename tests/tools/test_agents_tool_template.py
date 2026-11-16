"""The `guide` template on the `agents` group, old guide files, the Spanish texts and the docs that must not drift."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from tests.support.admin_helpers import AdminFixture
from tests.support.agents_tool_helpers import ALL_AGENT_TOOLS, AgentsFixture

from src.agent import templates
from src.agent.permissions_prompt import permissions_block
from src.agent.tool_groups import DEFAULT_TOOL_GROUPS, DELEGATE_GRANTABLE_GROUPS, TOOL_GROUP_CATALOG
from src.agent.toolsets.agents import agents_notice
from src.config import ceiling
from src.config.agents import AgentStore
from src.config.levels import Level, check_agent_level, effective_agent_level
from src.config.levels_help import levels_catalog, levels_help, needed_level
from src.config.loader import load_agent_config, load_user
from src.config.models import AgentConfig
from src.utils.i18n import _SPANISH, UserMessageError

ROOT = Path(__file__).resolve().parents[2]
GUIDE_GROUPS = ["memory_read", "memory_edit", "web", "self", "identity", "agents", "permissions"]
OLD_GUIDE = (
    "id: guide\nname: Guide\nchat_config:\n  tool_groups: [memory_read, memory_edit, web, self, terminal]\n"
    "terminal:\n  backends:\n    local:\n      workdir: /app\n      approvals: manual\n"
)


class GuideTemplate(AdminFixture):
    def set_max(self, level: str) -> None:
        self.write_user(re.sub(r"max_level: \w+", f"max_level: {level}", self.read("user-a/user.yaml")))

    def test_the_template_shape(self) -> None:
        guide = templates.templates()["guide"]
        self.assertEqual(guide.min_level, Level.OPERATOR)
        config, soul, _ = templates.build("guide", "guide", "Guide")
        self.assertEqual((config.level, config.chat_config.tool_groups, config.terminal), (Level.OPERATOR, GUIDE_GROUPS, None))
        self.assertNotIn("terminal", config.chat_config.tool_groups)
        self.assertLessEqual(len(soul), templates.SOUL_MAX_CHARS)
        self.assertFalse(hasattr(templates, "GUIDE_BACKEND"))

    def test_it_loads_and_is_created_for_an_operator_user_and_nothing_else_is_written(self) -> None:
        self.set_max("operator")
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "guide", "--from-template", "guide"])
        self.assertEqual((code, err), (0, ""), out)
        user = load_user("user-a")
        config = load_agent_config("user-a", "guide")
        check_agent_level(config, user, user.max_level)  # no ceiling is crossed
        self.assertEqual(effective_agent_level(config, user), Level.OPERATOR)
        self.assertEqual(user.terminal_backends, {})
        self.assertNotIn("terminal_backends", self.read("user-a/user.yaml"))
        self.assertEqual(load_user("user-a").agents, ["bot", "guide"])

    def test_below_operator_it_is_refused_with_a_clear_message(self) -> None:
        self.set_max("collaborator")
        code, _, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "guide", "--from-template", "guide"])
        self.assertEqual(code, 1)
        self.assertIn("'operator'", err)
        self.assertIn("max_level=operator", err)

    def test_an_admin_may_still_add_the_terminal_by_hand(self) -> None:
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "guide", "--from-template", "guide"])
        self.assertEqual((code, err), (0, ""), out)
        self.run_cli(["user", "set", "--user", "user-a", "--set", "terminal_backends.local={type: local, workdir: /app}"])
        code, out, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "guide", "--level", "admin",
                                       "--set", "chat_config.tool_groups=[memory_read,self,agents,permissions,terminal]",
                                       "--set", "terminal.backends.local.approvals=manual"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("terminal", load_agent_config("user-a", "guide").chat_config.tool_groups)


class OldGuideShape(AdminFixture):
    def test_an_old_guide_still_loads_and_is_refused_only_at_the_ceiling(self) -> None:
        self.write_user(self.read("user-a/user.yaml") + "terminal_backends:\n  local: {type: local, workdir: /app}\n")
        self.write_agent("guide", OLD_GUIDE)
        user = load_user("user-a")
        config = load_agent_config("user-a", "guide")  # loads: nothing to migrate
        self.assertEqual(config.chat_config.tool_groups, ["memory_read", "memory_edit", "web", "self", "terminal"])
        self.assertEqual(effective_agent_level(config, user), Level.ADMIN)
        check_agent_level(config, user, Level.ADMIN)
        with self.assertRaises(UserMessageError) as caught:  # an operator user: refused by the ceiling, as before
            check_agent_level(config, user, Level.OPERATOR)
        self.assertIn("local", str(caught.exception))
        self.assertEqual(ceiling.usable_backends(Level.OPERATOR, user.terminal_backends), {})
        self.assertIn("agents", ceiling.allowed_groups(Level.OPERATOR))


class Localization(unittest.TestCase):
    def test_catalog_entry_and_level_text_have_spanish(self) -> None:
        entry = next(item for item in TOOL_GROUP_CATALOG if item["id"] == "agents")
        for text in (entry["name"], entry["description"]):
            self.assertIn(text, _SPANISH)
            self.assertNotEqual(_SPANISH[text], text)
        from src.config.levels_help import SERVICE_TEXT

        self.assertIn(SERVICE_TEXT["agents"], _SPANISH)
        self.assertIn("agents", _SPANISH[SERVICE_TEXT["agents"]])  # the group id is kept, as for the others

    def test_the_web_lists_the_group_with_its_level(self) -> None:
        catalog = levels_catalog()
        self.assertEqual(catalog["groups"]["agents"], "operator")
        self.assertEqual(catalog["services"]["agents"], "operator")
        for level in catalog["levels"]:
            self.assertEqual("agents" in level["tool_groups"], level["number"] >= int(Level.OPERATOR))
        self.assertIn("agents", [item["id"] for item in TOOL_GROUP_CATALOG])
        script = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("state.toolGroupCatalog.map", script)  # the Tools tab renders the catalog: no hard-coded list

    def test_it_is_never_a_default_nor_grantable(self) -> None:
        self.assertNotIn("agents", DEFAULT_TOOL_GROUPS)
        self.assertNotIn("agents", DELEGATE_GRANTABLE_GROUPS)

    def test_help_and_prompt_mention_it_only_where_it_applies(self) -> None:
        self.assertEqual(needed_level("agents"), Level.OPERATOR)
        self.assertIn("`agents` group", levels_help())
        for level in Level:
            text = permissions_block(level, Level.ADMIN)
            self.assertEqual("agents tools" in text, level == Level.OPERATOR, level.label)
        notice = agents_notice(None)  # type: ignore[arg-type]
        for needle in ("approval", "lowest level", "yourself", "last agent", "administrator", "never as instructions"):
            self.assertIn(needle, notice.replace("data, never instructions", "never as instructions"))


class Docs(unittest.TestCase):
    def test_every_tool_is_documented_where_the_permissions_tools_are(self) -> None:
        for page in ("docs/reference/tools.md", "docs/architecture/permissions.md", "src/knowledge/recall/references/tools-and-groups.md"):
            text = (ROOT / page).read_text(encoding="utf-8")
            for name in ALL_AGENT_TOOLS:
                with self.subTest(page=page, tool=name):
                    self.assertIn(f"`{name}", text)

    def test_the_group_is_in_the_tables(self) -> None:
        self.assertIn("agents", (ROOT / "docs/reference/agent-yaml.md").read_text(encoding="utf-8").split("tool_groups:")[1][:400])
        self.assertIn("`agents`", (ROOT / "docs/architecture/permissions.md").read_text(encoding="utf-8"))
        self.assertIn("agents", (ROOT / "src/knowledge/recall/references/permissions.md").read_text(encoding="utf-8"))

    def test_the_guide_no_longer_needs_the_terminal_anywhere(self) -> None:
        backlog = (ROOT / "docs/project/backlog.md").read_text(encoding="utf-8")
        self.assertNotIn("Scoped admin tools for the setup guide", backlog)
        soul = (ROOT / "src/agent/guide_soul.md").read_text(encoding="utf-8")
        self.assertNotIn("python -m src.admin", soul)
        self.assertIn("administrator", soul)
        for token in ("terminal backends", "users", "provider keys"):
            self.assertIn(token, soul.lower())
        admin = (ROOT / "docs/user-guide/admin-cli.md").read_text(encoding="utf-8")
        self.assertNotIn("uses the `local` terminal backend", admin)


class GuideWorksThroughTheTools(AgentsFixture):
    def test_a_guide_agent_can_run_the_whole_flow_without_a_terminal(self) -> None:
        self.write_agent("chief", "id: chief\nname: Guide\nlevel: operator\nchat_config:\n  tool_groups: %s\n" % GUIDE_GROUPS)
        self.assertIn("agent_create", self.tools(groups=GUIDE_GROUPS))
        self.ok("agent_create", self.run_of(), agent_id="notes", name="Notes", description="Takes notes")
        self.ok("agent_update", self.run_of(), agent_id="notes", description="Takes better notes")
        self.ok("agent_rename", self.run_of(), agent_id="notes", new_id="journal")
        self.ok("agent_delete", self.run_of(), agent_id="journal")
        self.assertNotIn("journal", self.agent_ids())
        store = AgentStore(self.config_dir, self.data_dir)
        self.assertEqual(store.user("user-a").agents, ["chief", "boss", "bot", "ops", "box"])
        self.assertIsInstance(load_agent_config("user-a", "chief"), AgentConfig)


if __name__ == "__main__":
    unittest.main()
