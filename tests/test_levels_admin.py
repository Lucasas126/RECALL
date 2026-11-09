"""Permission levels, step 1: admin CLI (create-user, user set, agent create/set/check, migrate-levels)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from admin_helpers import AdminFixture

from src.config.levels import Level, forget_warnings
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
        forget_warnings()
        self.addCleanup(forget_warnings)
        import src.admin.levels  # noqa: F401  (imported lazily by the CLI; its directories are patched like the others)

        for target, value in (("src.admin.levels.CONFIG_DIR", self.config_dir), ("src.admin.levels.DATA_DIR", self.data_dir)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def set_max(self, level: str | None) -> None:
        text = self.read("user-a/user.yaml").replace("max_level: admin\n", "")
        self.write_user(text if level is None else text + f"max_level: {level}\n")

    def add_user(self, user_id: str = "user-b") -> None:
        folder = self.config_dir / user_id
        folder.mkdir()
        (folder / "user.yaml").write_text(
            f"id: {user_id}\nname: B\nprovider: {{name: ollama}}\nagents: []\n", encoding="utf-8",
        )

    def yaml_of(self, relative: str) -> dict:
        return yaml.safe_load(self.read(relative))


class CreateUserTests(LevelAdminBase):
    def create(self, *extra: str, user_id: str = "user-c"):
        return self.run_cli(["create-user", user_id, "--api-key-env", "K", *extra], passwords=[GOOD, GOOD])

    def test_first_user_of_an_installation_is_admin(self) -> None:
        for path in (self.config_dir / "user-a").glob("**/*"):
            if path.is_file():
                path.unlink()
        (self.config_dir / "user-a" / "agents").rmdir()
        (self.config_dir / "user-a").rmdir()
        code, out, err = self.create(user_id="first")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.yaml_of("first/user.yaml")["max_level"], "admin")

    def test_later_users_are_collaborators_unless_told_otherwise(self) -> None:
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
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "mid", "--level", "reader"])
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
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\n")
        code, out, err = self.run_cli(["agent", "rename", "--user", "user-a", "--agent", "bot", "--new-id", "bot2"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIs(load_agent_config("user-a", "bot2").level, Level.READER)

    def test_list_shows_the_effective_level(self) -> None:
        code, out, _ = self.run_cli(["agent", "list", "--user", "user-a"])
        self.assertEqual(code, 0)
        self.assertIn("level=assistant", out)  # tool groups default to memory_read, memory_edit, web, self


class AgentCheckTests(LevelAdminBase):
    def test_prints_each_effective_level(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\n")
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 0, out)
        self.assertIn("level: user-a/bot = reader (declared)", out)
        self.write_agent("bot", "id: bot\nname: Bot\n")
        code, out, _ = self.run_cli(["agent", "check"])
        self.assertIn("level: user-a/bot = assistant (computed, needs assistant)", out)

    def test_warns_when_an_undeclared_agent_needs_more_than_the_maximum(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator") + SSH_BACKENDS)
        self.write_agent("bot", TERMINAL_AGENT.format(id="bot"))
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 0, out)
        self.assertIn("= admin (computed, needs admin)", out)
        self.assertIn("warning: user-a/bot: no level, and its configuration needs admin", out)

    def test_a_declared_level_above_the_maximum_is_an_error(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: admin\n")
        self.set_max("reader")
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 1)
        self.assertIn("error: user-a/bot: level admin is above the user's max_level reader", out)

    def test_warns_about_a_user_without_max_level(self) -> None:
        self.set_max(None)
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 0, out)
        self.assertIn("warning: user-a/user.yaml: no max_level; using admin", out)


class MigrateLevelsTests(LevelAdminBase):
    def setUp(self) -> None:
        super().setUp()
        self.set_max(None)
        self.write_user(self.read("user-a/user.yaml") + "# keep this comment\n" + SSH_BACKENDS)
        self.write_user(self.read("user-a/user.yaml").replace("agents: [bot]", "agents: [bot, term]"))
        self.write_agent("bot", "# agent comment\nid: bot\nname: Bot\n")
        self.write_agent("term", TERMINAL_AGENT.format(id="term"))

    def snapshot(self) -> dict[str, str]:
        return {str(p.relative_to(self.config_dir)): p.read_text(encoding="utf-8")
                for p in sorted(self.config_dir.rglob("*.yaml"))}

    def test_dry_run_writes_nothing(self) -> None:
        before = self.snapshot()
        code, out, err = self.run_cli(["migrate-levels", "--dry-run"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.snapshot(), before)
        self.assertIn("would set", out)
        self.assertIn("dry run", out)

    def test_single_user_gets_admin_and_agents_their_required_level(self) -> None:
        code, out, err = self.run_cli(["migrate-levels"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.yaml_of("user-a/user.yaml")["max_level"], "admin")
        self.assertEqual(self.yaml_of("user-a/agents/bot.yaml")["level"], "assistant")
        self.assertEqual(self.yaml_of("user-a/agents/term.yaml")["level"], "admin")
        self.assertIn("# keep this comment", self.read("user-a/user.yaml"))
        self.assertIn("# agent comment", self.read("user-a/agents/bot.yaml"))
        self.assertIs(load_agent_config("user-a", "term").level, Level.ADMIN)

    def test_is_idempotent(self) -> None:
        self.run_cli(["migrate-levels"])
        after_first = self.snapshot()
        code, out, err = self.run_cli(["migrate-levels"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.snapshot(), after_first)
        self.assertIn("changed 0 file(s)", out)

    def test_several_users_get_collaborator_and_a_conflict_is_reported_not_fatal(self) -> None:
        self.add_user()
        code, out, err = self.run_cli(["migrate-levels"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(self.yaml_of("user-a/user.yaml")["max_level"], "collaborator")
        self.assertEqual(self.yaml_of("user-b/user.yaml")["max_level"], "collaborator")
        self.assertEqual(self.yaml_of("user-a/agents/bot.yaml")["level"], "assistant")
        self.assertNotIn("level", self.yaml_of("user-a/agents/term.yaml"))  # left untouched
        self.assertIn("conflict: user=user-a agent=term required=admin max_level=collaborator", out)
        self.assertIn("1 conflict(s)", out)
        before = self.snapshot()
        self.run_cli(["migrate-levels"])
        self.assertEqual(self.snapshot(), before)

    def test_a_declared_max_level_is_kept(self) -> None:
        self.write_user(self.read("user-a/user.yaml") + "max_level: operator\n")
        code, out, _ = self.run_cli(["migrate-levels"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.yaml_of("user-a/user.yaml")["max_level"], "operator")
        self.assertIn("conflict: user=user-a agent=term required=admin max_level=operator", out)

    def test_an_invalid_agent_file_is_an_error_and_the_others_still_migrate(self) -> None:
        self.write_agent("broken", "id: broken\nname: B\nunknown_key: 1\n")
        code, out, err = self.run_cli(["migrate-levels"])
        self.assertEqual(code, 1)
        self.assertIn("broken.yaml", err)
        self.assertEqual(self.yaml_of("user-a/agents/bot.yaml")["level"], "assistant")


class ExampleConfigTests(unittest.TestCase):
    def test_tracked_example_declares_a_max_level(self) -> None:
        root = Path(__file__).resolve().parents[1]
        data = yaml.safe_load((root / ".config" / "example_user" / "user.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["max_level"], "collaborator")


if __name__ == "__main__":
    unittest.main()
