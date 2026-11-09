"""Permission levels, step 1: ordering, parsing, required levels, fallbacks and load-time validation."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from admin_helpers import AdminFixture
from pydantic import ValidationError

from src.config.agents import AgentStore
from src.config.levels import (
    Level, LevelExceedsMax, effective_agent_level, effective_user_level, forget_warnings, parse_level, required_level,
)
from src.config.loader import load_agent
from src.config.models import AgentConfig, UserConfig

SSH = {"type": "ssh", "host": "box.example", "user": "deploy", "key_file": "/k", "known_hosts_file": "/h"}


def agent(groups=None, *, terminal=None, mcp=None, delegation=None, tools=True, level=None) -> AgentConfig:
    chat: dict = {"tools": tools}
    if groups is not None:
        chat["tool_groups"] = groups
    if mcp:
        chat["mcp"] = {name: {} for name in mcp}
    data: dict = {"id": "bot", "name": "Bot", "chat_config": chat}
    if terminal:
        data["terminal"] = {"backends": {name: {} for name in terminal}, "default_backend": terminal[0]}
    if delegation is not None:
        data["delegation"] = delegation
    if level is not None:
        data["level"] = level
    return AgentConfig.model_validate(data)


def user_config(**extra) -> UserConfig:
    backends = {
        "sandbox": SSH, "pc": {**SSH, "user": "user-a"}, "orion": {**SSH, "user": "root"},
        "local": {"type": "local", "workdir": "/app"},
    }
    return UserConfig.model_validate({
        "id": "user-a", "name": "User A", "provider": {"name": "ollama"}, "agents": [],
        "terminal_backends": backends, **extra,
    })


class LevelTypeTests(unittest.TestCase):
    def test_levels_are_ordered(self) -> None:
        names = [level.label for level in sorted(Level)]
        self.assertEqual(names, ["minimal", "reader", "assistant", "collaborator", "operator", "admin"])
        self.assertEqual([int(level) for level in Level], [0, 1, 2, 3, 4, 5])
        self.assertLess(Level.OPERATOR, Level.ADMIN)

    def test_parsing_is_case_insensitive_and_strict(self) -> None:
        self.assertIs(parse_level("Operator"), Level.OPERATOR)
        self.assertIs(parse_level(" ADMIN "), Level.ADMIN)
        self.assertIs(parse_level(Level.READER), Level.READER)
        for bad in ("root", "", 3, True, None, "4"):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                parse_level(bad)

    def test_models_accept_names_and_write_the_lowercase_name(self) -> None:
        config = AgentConfig.model_validate({"id": "a", "name": "A", "level": "Operator"})
        self.assertIs(config.level, Level.OPERATOR)
        self.assertEqual(config.model_dump(mode="json")["level"], "operator")
        self.assertEqual(user_config(max_level="COLLABORATOR").model_dump(mode="json")["max_level"], "collaborator")
        self.assertIsNone(AgentConfig(id="a", name="A").level)
        with self.assertRaises(ValidationError):
            AgentConfig.model_validate({"id": "a", "name": "A", "level": "god"})
        with self.assertRaises(ValidationError):
            AgentConfig.model_validate({"id": "a", "name": "A", "level": 3})


class RequiredLevelTests(unittest.TestCase):
    def required(self, config: AgentConfig) -> Level:
        return required_level(config, user_config().terminal_backends)

    def test_each_service_class(self) -> None:
        cases = [
            (agent([]), Level.MINIMAL),
            (agent(tools=False), Level.MINIMAL),
            (agent(["memory_read"]), Level.READER),
            (agent(["folders_read"]), Level.READER),
            (agent(["memory_edit"]), Level.ASSISTANT),
            (agent(["self"]), Level.ASSISTANT),
            (agent(["web"]), Level.ASSISTANT),
            (agent(["folders_edit"]), Level.COLLABORATOR),
            (agent(["delegate"]), Level.COLLABORATOR),
            (agent(["memory_read"], mcp=["notes"]), Level.COLLABORATOR),
            (agent(["terminal"], terminal=["sandbox"]), Level.OPERATOR),
            (agent(["terminal"], terminal=["pc"]), Level.OPERATOR),
            (agent(["terminal"], terminal=["orion"]), Level.ADMIN),
            (agent(["terminal"], terminal=["local"]), Level.ADMIN),
            (agent(["delegate"], delegation={"targets": [{"agent": "other"}]}), Level.OPERATOR),
        ]
        for config, expected in cases:
            with self.subTest(groups=config.chat_config.tool_groups, expected=expected):
                self.assertIs(self.required(config), expected)

    def test_groups_default_when_unset(self) -> None:
        self.assertIs(self.required(agent()), Level.ASSISTANT)  # memory_read, memory_edit, web, self

    def test_a_delegation_block_without_the_group_or_disabled_adds_nothing(self) -> None:
        targets = {"targets": [{"agent": "other"}]}
        self.assertIs(self.required(agent(["web"], delegation=targets)), Level.ASSISTANT)
        self.assertIs(self.required(agent(["delegate"], delegation={**targets, "enabled": False})), Level.COLLABORATOR)

    def test_unknown_backend_names_are_ignored_but_the_group_needs_operator(self) -> None:
        self.assertIs(self.required(agent(["terminal"], terminal=["gone"])), Level.OPERATOR)

    def test_the_highest_requirement_wins(self) -> None:
        config = agent(["memory_read", "memory_edit", "web", "self", "terminal"], terminal=["sandbox", "orion"])
        self.assertIs(self.required(config), Level.ADMIN)

    def test_production_shape_is_admin(self) -> None:
        config = agent(
            ["memory_read", "memory_edit", "web", "self", "terminal"], terminal=["local", "sandbox", "pc", "orion"],
        )
        self.assertIs(effective_agent_level(config, user_config()), Level.ADMIN)


class EffectiveLevelTests(unittest.TestCase):
    def setUp(self) -> None:
        forget_warnings()
        self.addCleanup(forget_warnings)

    def test_declared_user_level_wins(self) -> None:
        self.assertIs(effective_user_level(user_config(max_level=Level.READER), 1), Level.READER)

    def test_single_user_installation_is_admin_several_are_collaborator(self) -> None:
        self.assertIs(effective_user_level(user_config(), 1), Level.ADMIN)
        self.assertIs(effective_user_level(user_config(), 2), Level.COLLABORATOR)

    def test_the_fallback_warns_once_per_user_and_names_the_commands(self) -> None:
        with self.assertLogs("recall.config", "WARNING") as logs:
            effective_user_level(user_config(), 2)
            effective_user_level(user_config(), 2)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("migrate-levels", logs.output[0])
        self.assertIn("max_level=", logs.output[0])

    def test_declared_user_level_does_not_warn(self) -> None:
        with self.assertNoLogs("recall.config", "WARNING"):
            effective_user_level(user_config(max_level=Level.OPERATOR), 3)

    def test_declared_agent_level_wins_over_required(self) -> None:
        config = agent(["terminal"], terminal=["orion"], level=Level.READER)
        self.assertIs(effective_agent_level(config, user_config()), Level.READER)


class LoaderTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        forget_warnings()
        self.addCleanup(forget_warnings)
        environment = patch.dict(os.environ, {"USER_A_KEY_NAME": "secret"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_resolved_agent_carries_both_levels(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator"))
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: collaborator\n")
        resolved = load_agent("user-a", "bot")
        self.assertEqual((resolved.level, resolved.max_level), (Level.COLLABORATOR, Level.OPERATOR))

    def test_missing_levels_are_resolved_at_load(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin\n", ""))
        resolved = load_agent("user-a", "bot")
        self.assertEqual((resolved.level, resolved.max_level), (Level.ASSISTANT, Level.ADMIN))  # single user
        other = self.config_dir / "user-b"
        other.mkdir()
        (other / "user.yaml").write_text("id: user-b\nname: B\nprovider: {name: ollama}\nagents: []\n", encoding="utf-8")
        self.assertEqual(load_agent("user-a", "bot").max_level, Level.COLLABORATOR)

    def test_declared_level_above_max_is_an_invalid_agent(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator"))
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: admin\n")
        with self.assertRaises(LevelExceedsMax) as raised:
            load_agent("user-a", "bot")
        self.assertEqual(raised.exception.code, "level_exceeds_max")
        self.assertIn("operator", str(raised.exception))

    def test_production_shape_for_a_lower_max_is_a_warning_not_a_failure(self) -> None:
        self.write_user(
            self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator")
            + "terminal_backends:\n"
            "  local: {type: local, workdir: /app}\n"
            "  sandbox: {type: ssh, host: a.example, user: deploy, key_file: /k, known_hosts_file: /h}\n"
            "  pc: {type: ssh, host: b.example, user: user-a, key_file: /k, known_hosts_file: /h}\n"
            "  orion: {type: ssh, host: c.example, user: root, key_file: /k, known_hosts_file: /h}\n"
        )
        self.write_agent(
            "bot",
            "id: bot\nname: Bot\nchat_config:\n  tool_groups: [memory_read, memory_edit, web, self, terminal]\n"
            "terminal:\n  default_backend: sandbox\n  backends: {local: {}, sandbox: {}, pc: {}, orion: {}}\n",
        )
        with self.assertLogs("recall.config", "WARNING") as logs:
            resolved = load_agent("user-a", "bot")
        self.assertEqual((resolved.level, resolved.max_level), (Level.ADMIN, Level.OPERATOR))
        self.assertTrue(any("needs admin" in line and "operator" in line for line in logs.output))

    def test_store_rejects_a_declared_level_above_max_on_create_and_update(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: assistant"))
        store = AgentStore(self.config_dir, self.data_dir)
        with self.assertRaises(LevelExceedsMax):
            store.create("user-a", AgentConfig(id="new", name="New", level=Level.OPERATOR))
        self.assertFalse((self.config_dir / "user-a" / "agents" / "new.yaml").exists())
        before = self.read("user-a/agents/bot.yaml")
        with self.assertRaises(LevelExceedsMax):
            store.update("user-a", "bot", AgentConfig(id="bot", name="Bot", level=Level.COLLABORATOR))
        self.assertEqual(self.read("user-a/agents/bot.yaml"), before)
        store.create("user-a", AgentConfig(id="ok", name="Ok", level=Level.ASSISTANT))

    def test_store_update_without_level_keeps_the_stored_one(self) -> None:
        store = AgentStore(self.config_dir, self.data_dir)
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: operator\n")
        store.update("user-a", "bot", AgentConfig(id="bot", name="Renamed"))
        self.assertIn("level: operator", self.read("user-a/agents/bot.yaml"))
        store.update("user-a", "bot", AgentConfig(id="bot", name="Renamed", level=Level.READER))
        self.assertIn("level: reader", self.read("user-a/agents/bot.yaml"))

    def test_service_rejects_and_preserves(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator"))
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: operator\n")
        with self.assertRaises(LevelExceedsMax):
            self.service.create_agent("user-a", AgentConfig(id="new", name="New", level=Level.ADMIN))
        with self.assertRaises(LevelExceedsMax):
            self.service.update_agent("user-a", "bot", AgentConfig(id="bot", name="Bot", level=Level.ADMIN))
        view = self.service.update_agent("user-a", "bot", AgentConfig(id="bot", name="Edited"))
        self.assertEqual(view["level"], "operator")
        self.assertIn("level: operator", self.read("user-a/agents/bot.yaml"))


if __name__ == "__main__":
    unittest.main()
