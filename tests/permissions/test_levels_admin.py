"""Permission levels: admin CLI (create-user, user set, agent create/set/check). No migration command exists."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from tests.support.admin_helpers import AdminFixture

from src.config.levels import Level
from src.config.loader import load_agent_config, load_user

GOOD = "correct-horse-battery"
SSH_BACKENDS = (
    "terminal_backends:\n"
    "  local: {type: local, workdir: /app}\n"
    "  orion: {type: ssh, host: c.example, user: root, key_file: /k, known_hosts_file: /h}\n"
)
TERMINAL_AGENT = (
    "id: {id}\nname: Term\nchat_config:\n  tool_groups: [memory_read, terminal]\n"
    "terminal:\n  backends: {{orion: {{}}}}\n"
)


class LevelAdminBase(AdminFixture):
    def setUp(self) -> None:
        super().setUp()

    def set_max(self, level: str) -> None:
        text = self.read("user-a/user.yaml").replace("max_level: admin\n", "")
        self.write_user(text + f"max_level: {level}\n")

    def add_user(self, user_id: str = "user-b") -> None:
        folder = self.config_dir / user_id
        folder.mkdir()
        (folder / "user.yaml").write_text(
            f"id: {user_id}\nname: B\nmax_level: collaborator\nprovider: {{name: ollama}}\nagents: []\n", encoding="utf-8",
        )

    def yaml_of(self, relative: str) -> dict:
        return yaml.safe_load(self.read(relative))


class CreateUserTests(LevelAdminBase):
    def create(self, *extra: str, user_id: str = "user-c"):
        return self.run_cli(["create-user", user_id, "--api-key-env", "K", *extra], passwords=[GOOD, GOOD])

    def test_the_first_user_is_not_special(self) -> None:
        for path in (self.config_dir / "user-a").glob("**/*"):
            if path.is_file():
                path.unlink()
        (self.config_dir / "user-a" / "agents").rmdir()
        (self.config_dir / "user-a").rmdir()
        code, out, err = self.create(user_id="first")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.yaml_of("first/user.yaml")["max_level"], "collaborator")

    def test_users_are_collaborators_unless_told_otherwise(self) -> None:
        code, out, err = self.create()
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.yaml_of("user-c/user.yaml")["max_level"], "collaborator")
        code, out, err = self.create("--max-level", "Operator", user_id="user-d")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.yaml_of("user-d/user.yaml")["max_level"], "operator")
        self.assertIs(load_user("user-d").max_level, Level.OPERATOR)

    def test_invalid_max_level_creates_nothing(self) -> None:
        code, _, err = self.create("--max-level", "root")
        self.assertEqual(code, 1)
        self.assertIn("Invalid --max-level", err)
        self.assertFalse((self.config_dir / "user-c").exists())


class UserSetTests(LevelAdminBase):
    def test_max_level_is_settable_and_written_in_lowercase(self) -> None:
        code, out, err = self.run_cli(["user", "set", "--user", "user-a", "--set", "max_level=Operator"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.yaml_of("user-a/user.yaml")["max_level"], "operator")

    def test_invalid_value_is_refused_and_nothing_is_written(self) -> None:
        before = self.read("user-a/user.yaml")
        code, _, err = self.run_cli(["user", "set", "--user", "user-a", "--set", "max_level=god"])
        self.assertEqual(code, 1)
        self.assertIn("max_level", err)
        self.assertEqual(self.read("user-a/user.yaml"), before)

    def test_lowering_below_a_declared_agent_level_is_refused(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: operator\n")
        before = self.read("user-a/user.yaml")
        code, _, err = self.run_cli(["user", "set", "--user", "user-a", "--set", "max_level=assistant"])
        self.assertEqual(code, 1)
        self.assertIn("bot (operator)", err)
        self.assertEqual(self.read("user-a/user.yaml"), before)
        code, _, err = self.run_cli(["user", "set", "--user", "user-a", "--set", "max_level=operator"])
        self.assertEqual((code, err), (0, ""))


class AgentCommandTests(LevelAdminBase):
    def test_create_with_level_and_above_max(self) -> None:
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "mid", "--level", "reader", "--set", "chat_config.tool_groups=[memory_read]"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIs(load_agent_config("user-a", "mid").level, Level.READER)
        self.set_max("assistant")
        code, _, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "high", "--level", "operator"])
        self.assertEqual(code, 1)
        self.assertIn("above your maximum level 'assistant'", err)
        self.assertFalse((self.config_dir / "user-a" / "agents" / "high.yaml").exists())
        self.assertEqual(load_user("user-a").agents, ["bot", "mid"])

    def test_set_level_validates_against_max(self) -> None:
        self.set_max("collaborator")
        code, out, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--level", "collaborator"])
        self.assertEqual((code, err), (0, ""), out)
        code, _, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set", "level=admin"])
        self.assertEqual(code, 1)
        self.assertIn("maximum level", err)
        self.assertIs(load_agent_config("user-a", "bot").level, Level.COLLABORATOR)

    def test_other_settings_keep_the_level_and_do_not_declare_one(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: operator\n")
        code, out, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set", "description=x"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIs(load_agent_config("user-a", "bot").level, Level.OPERATOR)
        self.write_agent("plain", "id: plain\nname: Plain\n")
        self.write_user(self.read("user-a/user.yaml").replace("agents: [bot]", "agents: [bot, plain]"))
        code, out, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "plain", "--set", "description=x"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIsNone(load_agent_config("user-a", "plain").level)  # effective levels are never written back

    def test_rename_keeps_the_level(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\nchat_config:\n  tool_groups: [memory_read]\n")
        code, out, err = self.run_cli(["agent", "rename", "--user", "user-a", "--agent", "bot", "--new-id", "bot2"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIs(load_agent_config("user-a", "bot2").level, Level.READER)

    def test_list_shows_the_effective_level(self) -> None:
        code, out, _ = self.run_cli(["agent", "list", "--user", "user-a"])
        self.assertEqual(code, 0)
        self.assertIn("level=assistant", out)  # tool groups default to memory_read, memory_edit, web, self


class AgentCheckTests(LevelAdminBase):
    def test_prints_each_effective_level(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\nchat_config:\n  tool_groups: [memory_read]\n")
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 0, out)
        self.assertIn("level: user-a/bot = reader (declared)", out)
        self.write_agent("bot", "id: bot\nname: Bot\n")
        code, out, _ = self.run_cli(["agent", "check"])
        self.assertIn("level: user-a/bot = assistant (computed, needs assistant for tool group 'memory_edit')", out)

    def test_an_undeclared_agent_that_needs_more_than_the_maximum_is_an_error(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator") + SSH_BACKENDS)
        self.write_agent("bot", TERMINAL_AGENT.format(id="bot"))
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 1, out)
        self.assertIn("= admin (computed, needs admin for terminal backend 'orion' (ssh user root))", out)
        self.assertIn("error: user-a/bot: Agent 'bot' uses terminal backend 'orion' (ssh user root), which needs the level 'admin'", out)

    def test_a_declared_level_below_what_the_agent_needs_is_an_error(self) -> None:
        self.write_user(self.read("user-a/user.yaml") + SSH_BACKENDS)
        self.write_agent("bot", TERMINAL_AGENT.format(id="bot") + "level: operator\n")
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 1, out)
        self.assertIn("has the level 'operator', but terminal backend 'orion' (ssh user root) needs the level 'admin'", out)

    def test_a_declared_level_above_the_maximum_is_an_error(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: admin\n")
        self.set_max("reader")
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 1)
        self.assertIn("error: user-a/bot: The level 'admin' of agent 'bot' is above your maximum level 'reader'", out)

    def test_a_user_without_max_level_is_an_error_naming_the_command(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin\n", ""))
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 1, out)
        self.assertIn("user set --user user-a --set max_level=<level>", out)


class RemovedMigrationTests(LevelAdminBase):
    def test_migrate_levels_is_gone(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(["migrate-levels"])
        self.assertFalse((Path(__file__).resolve().parents[2] / "src" / "admin" / "levels.py").exists())


class GuideTemplateTests(LevelAdminBase):
    def test_guide_works_for_an_operator_user(self) -> None:
        self.set_max("operator")
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "guide", "--from-template", "guide"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(load_agent_config("user-a", "guide").level.label, "operator")
        self.assertNotIn("terminal_backends", self.read("user-a/user.yaml"))

    def test_guide_is_refused_below_operator(self) -> None:
        self.set_max("collaborator")
        before = self.read("user-a/user.yaml")
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "guide", "--from-template", "guide"])
        self.assertEqual(code, 1, out)
        self.assertIn("'operator'", err)
        self.assertIn("'collaborator'", err)
        self.assertEqual(self.read("user-a/user.yaml"), before)
        self.assertFalse((self.config_dir / "user-a" / "agents" / "guide.yaml").exists())


class ExampleConfigTests(unittest.TestCase):
    def test_tracked_example_declares_a_max_level(self) -> None:
        root = Path(__file__).resolve().parents[2]
        data = yaml.safe_load((root / ".config" / "example_user" / "user.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["max_level"], "collaborator")


if __name__ == "__main__":
    unittest.main()
