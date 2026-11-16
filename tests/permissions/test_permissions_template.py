"""The `permission-manager` agent template and its admin CLI entry."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from tests.support.admin_helpers import AdminFixture

from src.agent import templates
from src.agent.tool_groups import TOOL_GROUP_IDS, TOOL_GROUP_TOOLS
from src.config.levels import Level, effective_agent_level
from src.config.loader import load_agent_config, load_user
from src.core.sources.registry import SourceRegistry

NAME = "permission-manager"


class TemplateTests(unittest.TestCase):
    def test_config_is_valid_operator_with_the_chosen_groups_and_no_terminal(self) -> None:
        config, _, chosen = templates.build(NAME, "pm", "Permission manager")
        self.assertEqual(config.level, Level.OPERATOR)
        self.assertEqual(config.chat_config.tool_groups, ["memory_read", "memory_edit", "self", "identity", "permissions"])
        self.assertTrue(set(config.chat_config.tool_groups) <= TOOL_GROUP_IDS)
        self.assertNotIn("terminal", config.chat_config.tool_groups)
        self.assertNotIn("web", config.chat_config.tool_groups)
        self.assertIsNone(config.terminal)
        self.assertEqual(chosen.min_level, Level.OPERATOR)

    def test_the_guide_template_is_an_operator_without_terminal(self) -> None:
        guide = templates.templates()["guide"]
        self.assertEqual(guide.min_level, Level.OPERATOR)
        self.assertEqual(guide.config["level"], "operator")
        self.assertEqual(guide.config["chat_config"]["tool_groups"], ["memory_read", "memory_edit", "web", "self", "identity", "agents", "permissions"])
        self.assertNotIn("terminal", guide.config)

    def test_soul_is_short_english_and_names_real_tools(self) -> None:
        soul = templates.templates()[NAME].soul
        self.assertLessEqual(len(soul), templates.SOUL_MAX_CHARS)
        tools = {name for names in TOOL_GROUP_TOOLS.values() for name in names}
        mentioned = {token for token in re.findall(r"`([^`\n]+)`", soul) if re.fullmatch(r"[a-z]+(?:_[a-z]+)+", token)}
        self.assertTrue(mentioned and mentioned <= tools, mentioned - tools)
        lowered = soul.lower()
        for phrase in ("lowest level", "trade-off", "never try to go around", "approval", "data, never instructions"):
            self.assertIn(phrase, lowered)

    def test_unknown_template_lists_the_new_one(self) -> None:
        with self.assertRaises(ValueError) as caught:
            templates.build("nope", "x", "X")
        self.assertIn(NAME, str(caught.exception))


class AdminCreate(AdminFixture):
    def create(self, *extra: str):
        return self.run_cli(["agent", "create", "--user", "user-a", "--id", "pm", "--from-template", NAME, *extra])

    def test_create_for_an_admin_user(self) -> None:
        code, out, err = self.create()
        self.assertEqual((code, err), (0, ""), out)
        config = load_agent_config("user-a", "pm")
        self.assertEqual((config.name, config.level), ("Permission manager", Level.OPERATOR))
        self.assertIn("job is to explain permission levels", SourceRegistry.for_agent("user-a", "pm", data_dir=self.data_dir).agent_files().soul())
        self.assertEqual(effective_agent_level(config, load_user("user-a")), Level.OPERATOR)
        self.assertNotIn("terminal_backends", self.read("user-a/user.yaml"))  # no backend is added

    def test_create_for_an_operator_user(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator"))
        code, out, err = self.create("--name", "Perms")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(load_agent_config("user-a", "pm").name, "Perms")

    def test_a_user_below_operator_gets_a_clear_message_and_nothing_is_written(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: collaborator"))
        before = self.read("user-a/user.yaml")
        code, out, err = self.create()
        self.assertEqual(code, 1, out)
        for text in ("permission-manager", "'operator'", "'collaborator'", "max_level=operator", "Nothing was written"):
            self.assertIn(text, err)
        self.assertEqual(self.read("user-a/user.yaml"), before)
        self.assertFalse((self.config_dir / "user-a" / "agents" / "pm.yaml").exists())

    def test_a_lower_level_flag_is_refused_because_of_the_group(self) -> None:
        code, _, err = self.create("--level", "assistant")
        self.assertEqual(code, 1)
        self.assertIn("permissions", err)
        self.assertFalse((self.config_dir / "user-a" / "agents" / "pm.yaml").exists())

    def test_the_cli_help_lists_the_template(self) -> None:
        from src.admin import agents

        text = Path(agents.__file__).read_text(encoding="utf-8")
        self.assertIn("permission-manager", text.split("from __future__")[0])


if __name__ == "__main__":
    unittest.main()
