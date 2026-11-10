from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from admin_helpers import AdminFixture

from src.admin import agents as agent_cli
from src.admin.users import AdminError
from src.agent import templates
from src.agent.tool_groups import TOOL_GROUP_IDS, TOOL_GROUP_TOOLS
from src.config.loader import load_agent_config, load_user
from src.config.models import AgentConfig
from src.core.sources.registry import SourceRegistry


class DottedPathTests(unittest.TestCase):
    def test_values_are_parsed_as_yaml(self) -> None:
        data = agent_cli.apply_assignments({"id": "a"}, [
            "chat_config.tool_groups=[memory_read, web]", "chat_config.stream=false", "chat_config.model=openrouter:v/m",
            "terminal.backends.local.approvals=manual", "description=null", "dream.x={a: 1}",
        ])
        self.assertEqual(data["chat_config"]["tool_groups"], ["memory_read", "web"])
        self.assertIs(data["chat_config"]["stream"], False)
        self.assertEqual(data["chat_config"]["model"], "openrouter:v/m")
        self.assertEqual(data["terminal"]["backends"]["local"]["approvals"], "manual")
        self.assertIsNone(data["description"])
        self.assertEqual(data["dream"]["x"], {"a": 1})

    def test_the_input_is_not_mutated_and_bad_paths_are_refused(self) -> None:
        original = {"a": {"b": 1}}
        agent_cli.apply_assignments(original, ["a.b=2"])
        self.assertEqual(original, {"a": {"b": 1}})
        for bad in ("novalue", "=x", "a..b=1", "a b=1", "a.b=[unclosed"):
            with self.subTest(bad=bad), self.assertRaises(AdminError):
                agent_cli.apply_assignments({}, [bad])
        with self.assertRaises(AdminError):  # a scalar in the way is not silently replaced
            agent_cli.apply_assignments({"a": 1}, ["a.b=2"])


class RawStringValueTests(unittest.TestCase):
    def test_string_fields_take_the_value_raw(self) -> None:
        from src.config.models import AgentConfig
        for value, expected in (("Veterinaria: salud y cuidado", "Veterinaria: salud y cuidado"),
                                ("'Vet: a'", "Vet: a"), ('"x # y"', "x # y"), ("null", "null"), ("[a, b]", "[a, b]")):
            with self.subTest(value=value):
                data = agent_cli.apply_assignments({}, [f"description={value}"], AgentConfig)
                self.assertEqual(data["description"], expected)

    def test_other_types_keep_yaml(self) -> None:
        from src.config.models import AgentConfig
        data = agent_cli.apply_assignments({}, ["chat_config.tool_groups=[web, self]", "chat_config.stream=false",
                                                "terminal.default_backend=null"], AgentConfig)
        self.assertEqual(data["chat_config"]["tool_groups"], ["web", "self"])
        self.assertIs(data["chat_config"]["stream"], False)
        self.assertIsNone(data["terminal"]["default_backend"])

    def test_unknown_type_falls_back_to_the_raw_string_when_yaml_does_not_validate(self) -> None:
        def validate(data):
            if not isinstance(data.get("x", {}).get("y"), str):
                raise AdminError("y must be a string")
            return data
        result = agent_cli.apply_validated({}, ["x.y=Vet: care"], None, validate)
        self.assertEqual(result["x"]["y"], "Vet: care")
        with self.assertRaises(AdminError) as ctx:
            agent_cli.apply_validated({}, ["x.y=[1, 2]"], None, lambda d: (_ for _ in ()).throw(AdminError("always bad")))
        self.assertIn("parsed as YAML", str(ctx.exception))


class AgentCliTests(AdminFixture):
    def test_set_description_with_a_colon_works_for_agent_and_create(self) -> None:
        code, out, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set",
                                       "description=Veterinaria: salud y cuidado"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(load_agent_config("user-a", "bot").description, "Veterinaria: salud y cuidado")
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "vet", "--set",
                                       "description=\"Vet: care\""])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(load_agent_config("user-a", "vet").description, "Vet: care")

    def test_list_and_show_print_the_loaded_yaml(self) -> None:
        code, out, _ = self.run_cli(["agent", "list", "--user", "user-a"])
        self.assertEqual(code, 0)
        self.assertIn("bot", out)
        code, out, _ = self.run_cli(["agent", "show", "--user", "user-a", "--agent", "bot"])
        self.assertEqual(code, 0)
        shown = yaml.safe_load(out)
        self.assertEqual((shown["id"], shown["name"]), ("bot", "Bot"))
        self.assertIn("chat_config", shown)  # defaults as the loader sees them

    def test_create_default_template_matches_the_web_flow(self) -> None:
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "notes", "--name", "Notes",
                                       "--set", "chat_config.tool_groups=[memory_read]"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(load_user("user-a").agents, ["bot", "notes"])
        self.assertEqual(load_agent_config("user-a", "notes").chat_config.tool_groups, ["memory_read"])
        soul = SourceRegistry.for_agent("user-a", "notes", data_dir=self.data_dir).agent_files().soul()
        self.assertIn("SOUL", soul)

    def test_create_guide_gives_the_agents_groups_soul_and_no_terminal(self) -> None:
        code, out, err = self.run_cli(["agent", "create", "--user", "user-a", "--id", "guide", "--from-template", "guide"])
        self.assertEqual((code, err), (0, ""), out)
        config = load_agent_config("user-a", "guide")
        self.assertEqual(config.name, "Guide")
        self.assertEqual(config.chat_config.tool_groups, ["memory_read", "memory_edit", "web", "self", "agents", "permissions"])
        self.assertIsNone(config.terminal)
        self.assertNotIn("terminal_backends", self.read("user-a/user.yaml"))  # no backend is added any more
        soul = SourceRegistry.for_agent("user-a", "guide", data_dir=self.data_dir).agent_files().soul()
        self.assertIn("setup guide", soul)
        self.assertEqual(load_user("user-a").agents, ["bot", "guide"])
        # the user.yaml rewrite keeps the agents list editable by the store (indented block list)
        self.service.create_agent("user-a", AgentConfig(id="late", name="Late"))
        self.assertEqual(load_user("user-a").agents, ["bot", "guide", "late"])

    def test_create_errors_leave_files_untouched(self) -> None:
        before = self.read("user-a/user.yaml")
        for argv in (
            ["--id", "bot"],                                            # already exists
            ["--id", "bad id"],                                         # invalid id
            ["--id", "x", "--from-template", "nope"],                   # unknown template
            ["--id", "x", "--set", "chat_config.tool_groups=7"],        # invalid value
            ["--id", "x", "--set", "unknown_field=1"],                  # extra key refused
        ):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(["agent", "create", "--user", "user-a", *argv])
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("error:"), err)
        self.assertEqual(self.read("user-a/user.yaml"), before)
        self.assertEqual(sorted(p.name for p in (self.config_dir / "user-a" / "agents").iterdir() if p.suffix == ".yaml"), ["bot.yaml"])

    def test_set_validates_before_writing(self) -> None:
        code, out, _ = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set", "chat_config.stream=false",
                                     "--set", "description=Helper"])
        self.assertEqual(code, 0, out)
        config = load_agent_config("user-a", "bot")
        self.assertFalse(config.chat_config.stream)
        self.assertEqual(config.description, "Helper")
        before = self.read("user-a/agents/bot.yaml")
        for assignment in ("chat_config.tool_groups=[nope]", "name=''", "typo=1", "terminal.backends=[]", "dream.tools=[terminal]"):
            with self.subTest(assignment=assignment):
                code, _, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set", assignment])
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("error:"), err)
        code, _, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set", "id=other"])
        self.assertEqual(code, 1)
        self.assertIn("rename", err)
        self.assertEqual(self.read("user-a/agents/bot.yaml"), before)

    def test_set_model_goes_through_the_service_model_check(self) -> None:
        code, out, _ = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set", "chat_config.model=ollama:llama3"])
        self.assertEqual(code, 0, out)
        self.assertEqual(load_agent_config("user-a", "bot").chat_config.model, "ollama:llama3")

    def test_set_is_refused_while_the_agent_is_busy(self) -> None:
        slot = self.service._slot("user-a", "bot")
        slot.active["chat"] = object()
        before = self.read("user-a/agents/bot.yaml")
        code, _, err = self.run_cli(["agent", "set", "--user", "user-a", "--agent", "bot", "--set", "description=x"])
        slot.active.clear()
        self.assertEqual(code, 1)
        self.assertIn("wait", err)
        self.assertEqual(self.read("user-a/agents/bot.yaml"), before)

    def test_rename_moves_the_data_folder_like_the_web_ui(self) -> None:
        folder = self.data_dir / "user-a" / "bot"
        (folder / "memories").mkdir(parents=True)
        (folder / "memories" / "note.md").write_text("keep me", encoding="utf-8")
        code, out, err = self.run_cli(["agent", "rename", "--user", "user-a", "--agent", "bot", "--new-id", "main", "--name", "Main"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertFalse(folder.exists())
        self.assertEqual((self.data_dir / "user-a" / "main" / "memories" / "note.md").read_text(encoding="utf-8"), "keep me")
        self.assertEqual(load_user("user-a").agents, ["main"])
        self.assertEqual(load_agent_config("user-a", "main").name, "Main")
        code, _, err = self.run_cli(["agent", "rename", "--user", "user-a", "--agent", "main", "--new-id", "bad id"])
        self.assertEqual(code, 1)

    def test_delete_needs_yes_and_keeps_data(self) -> None:
        folder = self.data_dir / "user-a" / "bot"
        folder.mkdir(parents=True)
        code, _, err = self.run_cli(["agent", "delete", "--user", "user-a", "--agent", "bot"])
        self.assertEqual(code, 1)
        self.assertIn("--yes", err)
        self.assertEqual(load_user("user-a").agents, ["bot"])
        code, out, _ = self.run_cli(["agent", "delete", "--user", "user-a", "--agent", "bot", "--yes"])
        self.assertEqual(code, 0, out)
        self.assertEqual(load_user("user-a").agents, [])
        self.assertTrue(folder.is_dir())

    def test_unknown_user_and_agent_give_readable_errors(self) -> None:
        for argv in (["agent", "list", "--user", "nobody"], ["agent", "show", "--user", "user-a", "--agent", "ghost"],
                     ["agent", "set", "--user", "user-a", "--agent", "ghost", "--set", "description=x"]):
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(argv)
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("error:"), err)


class CheckCommandTests(AdminFixture):
    def test_ok_for_a_valid_tree(self) -> None:
        code, out, _ = self.run_cli(["agent", "check"])
        self.assertEqual(code, 0)
        self.assertIn("OK: 1 user(s), 1 agent(s)", out)

    def test_precise_errors_and_exit_code_one(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nchat_config: {stream: perhaps}\ntypo: 1\n")
        code, out, _ = self.run_cli(["agent", "check", "--user", "user-a"])
        self.assertEqual(code, 1)
        self.assertIn("user-a/agents/bot.yaml", out)
        self.assertIn("typo", out)
        self.assertIn("FAILED", out)

    def test_broken_yaml_syntax_and_missing_agent_file(self) -> None:
        self.write_agent("bot", "id: bot\nname: [unclosed\n")
        code, out, _ = self.run_cli(["agent", "check"])
        self.assertEqual(code, 1)
        self.assertRegex(out, r"error: user-a/agents/bot\.yaml: .*line")
        self.write_agent("bot", "id: bot\nname: Bot\n")
        self.write_user(self.read("user-a/user.yaml").replace("agents: [bot]", "agents: [bot, ghost]"))
        code, out, _ = self.run_cli(["agent", "check"])
        self.assertEqual(code, 1)
        self.assertIn("ghost", out)

    def test_orphan_agent_files_and_unset_keys_are_warnings(self) -> None:
        self.write_agent("extra", "id: extra\nname: Extra\n")
        with patch.dict("os.environ", {}, clear=False):
            code, out, _ = self.run_cli(["agent", "check"])
        self.assertEqual(code, 0)
        self.assertIn("warning:", out)
        self.assertIn("extra", out)

    def test_terminal_group_without_backend_is_a_warning(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nchat_config: {tool_groups: [terminal]}\nterminal: {backends: {box: {}}}\n")
        code, out, _ = self.run_cli(["agent", "check"])
        self.assertEqual(code, 0)
        self.assertIn("no usable backend", out)


class UserCliTests(AdminFixture):
    def test_set_takes_string_fields_raw(self) -> None:
        code, out, err = self.run_cli(["user", "set", "--user", "user-a", "--set", "name=User A: la jefa"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(load_user("user-a").name, "User A: la jefa")

    def test_show_never_prints_the_hash_and_shows_key_names(self) -> None:
        code, out, _ = self.run_cli(["user", "show", "--user", "user-a"])
        self.assertEqual(code, 0)
        self.assertIn("USER_A_KEY_NAME", out)
        self.assertNotIn("argon2", out)
        self.assertNotIn("password_hash:", out.replace("# password_hash: set", ""))
        self.assertIn("# password_hash: set", out)

    def test_set_validates_and_keeps_the_hash(self) -> None:
        user_hash = load_user("user-a").password_hash
        code, out, err = self.run_cli(["user", "set", "--user", "user-a", "--set", "name=User A B", "--set", "language=es",
                                       "--set", "provider.api_key=OTHER_KEY_NAME",
                                       "--set", "terminal_backends.local={type: local, workdir: /app}"])
        self.assertEqual((code, err), (0, ""), out)
        user = load_user("user-a")
        self.assertEqual((user.name, user.language, user.provider.api_key), ("User A B", "es", "OTHER_KEY_NAME"))
        self.assertEqual(user.password_hash, user_hash)
        self.assertEqual(user.agents, ["bot"])
        self.assertNotIn(user_hash, out)

    def test_refused_paths_and_invalid_values_leave_the_file_untouched(self) -> None:
        before = self.read("user-a/user.yaml")
        for assignment in ("password_hash=x", "id=other", "agents=[]", "language=Klingon", "provider.api_key=sk-or-v1-abcdef0123456789",
                           "provider.api_key=has space", "provider.name=nope", "models=[1]"):
            with self.subTest(assignment=assignment):
                code, out, err = self.run_cli(["user", "set", "--user", "user-a", "--set", assignment])
                self.assertEqual(code, 1)
                self.assertTrue(err.startswith("error:"), err)
                self.assertNotIn("abcdef0123456789", out + err)
        self.assertEqual(self.read("user-a/user.yaml"), before)


class TemplateTests(unittest.TestCase):
    def test_guide_soul_is_short_and_names_real_things(self) -> None:
        guide = templates.templates()["guide"]
        self.assertLessEqual(len(guide.soul), templates.SOUL_MAX_CHARS)
        self.assertLessEqual(len(templates.templates()["default"].soul), templates.SOUL_MAX_CHARS)
        tools = {name for names in TOOL_GROUP_TOOLS.values() for name in names}
        for token in re.findall(r"`([^`\n]+)`", guide.soul):
            if re.fullmatch(r"[a-z]+_[a-z_]+", token):
                self.assertIn(token, tools, token)
        self.assertIn("skill_view('recall')", guide.soul)
        self.assertNotIn("python -m src.admin", guide.soul)  # no admin CLI: it needs the admin level
        self.assertIn("agent_create", guide.soul)
        self.assertIn("administrator", guide.soul)
        for page in ("recipes", "admin-cli", "agent-config"):
            text = (Path(__file__).resolve().parents[1] / "src" / "knowledge" / "recall" / "references" / f"{page}.md").read_text(encoding="utf-8")
            self.assertIn("DISPLAY NAME", text, page)

    def test_guide_config_is_valid_and_uses_known_groups(self) -> None:
        config, _, chosen = templates.build("guide", "guide", "Guide")
        self.assertIsNone(config.terminal)
        self.assertEqual(config.chat_config.tool_groups, ["memory_read", "memory_edit", "web", "self", "agents", "permissions"])
        self.assertTrue(set(config.chat_config.tool_groups) <= TOOL_GROUP_IDS)
        with self.assertRaises(ValueError):
            templates.build("nope", "x", "X")


if __name__ == "__main__":
    unittest.main()
