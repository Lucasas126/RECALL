"""Level ceilings: what every level allows (table-driven), the config rule and the production shapes."""

from __future__ import annotations

import unittest

from src.agent.tool_groups import TOOL_GROUP_IDS, TOOL_GROUP_TOOLS
from src.config import ceiling
from src.config.levels import (
    Level, LevelBelowRequired, LevelExceedsMax, LevelRequiredAboveMax, check_agent_level, required_level,
    required_level_reason,
)
from src.config.models import AgentConfig, UserConfig

L = Level

# the "Levels and services" table of docs/architecture/permissions.md: the first level that may hold each group
GROUP_FIRST_LEVEL = {
    "memory_read": L.READER, "folders_read": L.READER,
    "memory_edit": L.ASSISTANT, "self": L.ASSISTANT, "web": L.ASSISTANT,
    "folders_edit": L.COLLABORATOR, "delegate": L.COLLABORATOR,
    "terminal": L.OPERATOR, "permissions": L.OPERATOR, "agents": L.OPERATOR,
}
SSH = {"type": "ssh", "host": "box.example", "key_file": "/k", "known_hosts_file": "/h"}
BACKENDS = {
    "box": {**SSH, "user": "deploy"},      # unprivileged ssh: a sandbox or a real machine
    "orion": {**SSH, "user": "root"},       # root over ssh
    "local": {"type": "local", "workdir": "/app"},
}
PRODUCTION_GROUPS = ["memory_read", "memory_edit", "web", "self", "terminal"]


def user(max_level: str = "admin") -> UserConfig:
    return UserConfig.model_validate({
        "id": "user-a", "name": "A", "max_level": max_level, "provider": {"name": "ollama"}, "agents": [],
        "terminal_backends": BACKENDS,
    })


def agent(groups, backends=None, **extra) -> AgentConfig:
    data: dict = {"id": "bot", "name": "Bot", "chat_config": {"tool_groups": groups}, **extra}
    if backends:
        data["terminal"] = {"backends": {name: {} for name in backends}, "default_backend": backends[0]}
    return AgentConfig.model_validate(data)


class GroupTableTests(unittest.TestCase):
    def test_the_table_covers_every_tool_group(self) -> None:
        self.assertEqual(set(GROUP_FIRST_LEVEL), set(TOOL_GROUP_IDS))
        self.assertEqual(set(ceiling.GROUP_MIN_LEVEL), set(TOOL_GROUP_IDS))

    def test_allowed_groups_per_level(self) -> None:
        for level in Level:
            expected = {group for group, first in GROUP_FIRST_LEVEL.items() if level >= first}
            with self.subTest(level=level.label):
                self.assertEqual(ceiling.allowed_groups(level), expected)
                for group in TOOL_GROUP_IDS:
                    self.assertEqual(ceiling.group_allowed(level, group), group in expected)

    def test_mcp_workers_targets_and_terminal_classes_per_level(self) -> None:
        for level in Level:
            caps = ceiling.capabilities(level)
            with self.subTest(level=level.label):
                self.assertEqual(caps.mcp, level >= L.COLLABORATOR)
                self.assertEqual(caps.delegation_workers, level >= L.COLLABORATOR)
                self.assertEqual(caps.delegation_targets, level >= L.OPERATOR)
                self.assertEqual(caps.terminal_machine, level >= L.OPERATOR)
                self.assertEqual(caps.terminal_privileged, level >= L.ADMIN)
                self.assertEqual(set(caps.groups), {g for g, first in GROUP_FIRST_LEVEL.items() if level >= first})

    def test_backend_classes(self) -> None:
        classes = {name: UserConfig.model_validate({
            "id": "u", "name": "U", "max_level": "admin", "provider": {"name": "ollama"}, "agents": [],
            "terminal_backends": BACKENDS,
        }).terminal_backends[name] for name in BACKENDS}
        for level in Level:
            with self.subTest(level=level.label):
                self.assertEqual(ceiling.backend_allowed(level, classes["box"]), level >= L.OPERATOR)
                self.assertEqual(ceiling.backend_allowed(level, classes["orion"]), level >= L.ADMIN)
                self.assertEqual(ceiling.backend_allowed(level, classes["local"]), level >= L.ADMIN)
                self.assertEqual(set(ceiling.usable_backends(level, classes)), {
                    name for name, minimum in (("box", L.OPERATOR), ("orion", L.ADMIN), ("local", L.ADMIN)) if level >= minimum
                })

    def test_every_catalogued_tool_has_a_minimum_and_unknown_names_are_refused(self) -> None:
        for group, names in TOOL_GROUP_TOOLS.items():
            for name in names:
                needed = ceiling.tool_min_level(name)
                self.assertIsNotNone(needed, name)
                if name != "skill_view":
                    self.assertLessEqual(needed, ceiling.GROUP_MIN_LEVEL[group], name)
        self.assertIsNone(ceiling.tool_min_level("rm_rf"))
        for level in Level:
            self.assertFalse(ceiling.tool_allowed(level, "rm_rf"))
            self.assertTrue(ceiling.tool_allowed(level, "skill_view"))  # reading one's own skills is never gated

    def test_tool_gate_per_level(self) -> None:
        samples = {
            "search": L.READER, "folder_read": L.READER, "write": L.ASSISTANT, "soul_edit": L.ASSISTANT,
            "web_search": L.ASSISTANT, "folder_write": L.COLLABORATOR, "task_start": L.COLLABORATOR,
            "terminal": L.OPERATOR, "sandbox_write": L.OPERATOR,
        }
        for name, first in samples.items():
            for level in Level:
                with self.subTest(tool=name, level=level.label):
                    self.assertEqual(ceiling.tool_allowed(level, name), level >= first)

    def test_mcp_tools_need_collaborator(self) -> None:
        mcp = frozenset({"calendar__list"})
        for level in Level:
            self.assertEqual(ceiling.tool_allowed(level, "calendar__list", mcp), level >= L.COLLABORATOR)
            self.assertFalse(ceiling.tool_allowed(level, "calendar__list"))  # not an MCP tool of this turn

    def test_the_refusal_names_the_level_and_the_needed_one(self) -> None:
        text = ceiling.refusal(L.READER, "write")
        self.assertTrue(text.startswith("error:"))
        self.assertIn("'reader'", text)
        self.assertIn("'assistant'", text)
        self.assertIn("unknown tool", ceiling.refusal(L.ADMIN, "rm_rf"))


class WorkerLevelTests(unittest.TestCase):
    def test_a_worker_is_the_lower_of_its_own_and_the_launchers_level(self) -> None:
        for own in Level:
            for launcher in Level:
                self.assertEqual(ceiling.worker_level(own, launcher), min(own, launcher))
            self.assertEqual(ceiling.worker_level(own, None), own)

    def test_grantable_groups_follow_the_launchers_level(self) -> None:
        self.assertEqual(ceiling.grantable_groups(L.ASSISTANT, ["web"]), [])  # cannot launch workers at all
        self.assertEqual(ceiling.grantable_groups(L.COLLABORATOR, ["web", "terminal"]), ["web"])
        self.assertEqual(ceiling.grantable_groups(L.OPERATOR, ["web", "terminal"]), ["web", "terminal"])


class DreamTableTests(unittest.TestCase):
    def test_dream_capabilities_per_level(self) -> None:
        for level in Level:
            caps = ceiling.dream_capabilities(level)
            with self.subTest(level=level.label):
                self.assertEqual(caps.enabled, level >= L.READER)
                self.assertEqual(caps.memory_read, level >= L.READER)
                self.assertEqual(caps.folders_read, level >= L.READER)
                self.assertEqual(caps.memory_edit, level >= L.ASSISTANT)
                self.assertEqual(caps.self_files, level >= L.ASSISTANT)
                self.assertEqual(caps.web, level >= L.ASSISTANT)
                self.assertEqual(caps.mcp_read_only, level >= L.COLLABORATOR)
                self.assertEqual(caps.sandbox_read, level >= L.OPERATOR)
                self.assertFalse(caps.terminal_exec or caps.handoff or caps.permissions or caps.limited_delegate)

    def test_dream_tool_names_per_level(self) -> None:
        review = set(ceiling.DREAM_REVIEW_TOOLS)
        for level in Level:
            allowed = ceiling.dream_allowed_tools(level)
            with self.subTest(level=level.label):
                self.assertTrue(review <= allowed)
                self.assertEqual(set(TOOL_GROUP_TOOLS["memory_read"]) <= allowed, level >= L.READER)
                self.assertEqual(set(TOOL_GROUP_TOOLS["folders_read"]) <= allowed, level >= L.READER)
                self.assertEqual(set(TOOL_GROUP_TOOLS["memory_edit"]) <= allowed, level >= L.ASSISTANT)
                self.assertEqual(set(TOOL_GROUP_TOOLS["self"]) <= allowed, level >= L.ASSISTANT)
                self.assertEqual(set(TOOL_GROUP_TOOLS["web"]) <= allowed, level >= L.ASSISTANT)
                never = {"folder_write", "folder_append", "folder_edit", "folder_move", "folder_delete"}
                never |= set(TOOL_GROUP_TOOLS["terminal"]) | set(TOOL_GROUP_TOOLS["delegate"])
                self.assertFalse(allowed & never)


class ConfigRuleTests(unittest.TestCase):
    def test_required_level_with_a_reason(self) -> None:
        self.assertEqual(required_level_reason(agent(["memory_read"]), user().terminal_backends), (L.READER, "tool group 'memory_read'"))
        level, why = required_level_reason(agent(["terminal"], ["orion"]), user().terminal_backends)
        self.assertEqual((level, why), (L.ADMIN, "terminal backend 'orion' (ssh user root)"))
        self.assertEqual(required_level(agent([])), L.MINIMAL)

    def test_the_rule_for_every_declared_level_and_maximum(self) -> None:
        config = agent(["web"])  # needs assistant
        for maximum in Level:
            for declared in (None, *Level):
                subject = config if declared is None else config.model_copy(update={"level": declared})
                if declared is not None and declared > maximum:
                    expected = LevelExceedsMax
                elif L.ASSISTANT > maximum:
                    expected = LevelRequiredAboveMax
                elif declared is not None and declared < L.ASSISTANT:
                    expected = LevelBelowRequired
                else:
                    expected = None
                with self.subTest(maximum=maximum.label, declared=getattr(declared, "label", None)):
                    if expected is None:
                        check_agent_level(subject, user(maximum.label), maximum)
                    else:
                        with self.assertRaises(expected):
                            check_agent_level(subject, user(maximum.label), maximum)

    def test_messages_name_the_agent_the_service_and_the_level(self) -> None:
        with self.assertRaises(LevelBelowRequired) as raised:
            check_agent_level(agent(["folders_edit"], level="reader"), user(), L.ADMIN)
        message = str(raised.exception)
        for part in ("'bot'", "'reader'", "tool group 'folders_edit'", "'collaborator'"):
            self.assertIn(part, message)
        self.assertEqual(raised.exception.code, "level_below_required")
        with self.assertRaises(LevelRequiredAboveMax) as raised:
            check_agent_level(agent(["terminal"], ["local"]), user(), L.OPERATOR)
        self.assertIn("terminal backend 'local' (type local)", str(raised.exception))
        self.assertIn("'admin'", str(raised.exception))
        self.assertIn("'operator'", str(raised.exception))
        self.assertEqual(raised.exception.code, "level_required_above_max")

    def test_mcp_and_delegation_targets_set_the_requirement(self) -> None:
        with_mcp = AgentConfig.model_validate(
            {"id": "m", "name": "M", "chat_config": {"tool_groups": ["memory_read"], "mcp": {"calendar": {}}}}
        )
        self.assertEqual(required_level_reason(with_mcp), (L.COLLABORATOR, "chat_config.mcp"))
        delegating = AgentConfig.model_validate({
            "id": "d", "name": "D", "chat_config": {"tool_groups": ["delegate"]},
            "delegation": {"targets": [{"agent": "other"}]},
        })
        self.assertEqual(required_level_reason(delegating), (L.OPERATOR, "delegation.targets"))


class ProductionShapeTests(unittest.TestCase):
    """The two real configurations that motivated the rule."""

    def test_groups_memory_edit_web_self_terminal_with_local_and_root_backends_are_admin_only(self) -> None:
        config = agent(PRODUCTION_GROUPS, ["local", "box", "orion"])
        self.assertEqual(required_level(config, user().terminal_backends), L.ADMIN)
        for maximum in (L.ASSISTANT, L.COLLABORATOR, L.OPERATOR):
            with self.assertRaises(LevelRequiredAboveMax):
                check_agent_level(config, user(maximum.label), maximum)
        check_agent_level(config, user(), L.ADMIN)
        check_agent_level(config.model_copy(update={"level": L.ADMIN}), user(), L.ADMIN)

    def test_the_same_agent_with_an_unprivileged_ssh_backend_is_operator(self) -> None:
        config = agent(PRODUCTION_GROUPS, ["box"])
        self.assertEqual(required_level(config, user().terminal_backends), L.OPERATOR)
        check_agent_level(config, user("operator"), L.OPERATOR)
        with self.assertRaises(LevelRequiredAboveMax):
            check_agent_level(config, user("collaborator"), L.COLLABORATOR)
        with self.assertRaises(LevelBelowRequired):
            check_agent_level(config.model_copy(update={"level": L.COLLABORATOR}), user(), L.ADMIN)


if __name__ == "__main__":
    unittest.main()
