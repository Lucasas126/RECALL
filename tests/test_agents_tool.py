"""The `agents` tools end to end: level matrix, ownership, cards, applied changes, protections and the audit trail."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from agents_tool_helpers import CHANGE_TOOLS, PNG, READ_TOOLS, AgentsFixture, AgentsRun
from permissions_tool_helpers import Run, resolved

from src.agent import templates
from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_IDS, TOOL_GROUP_TOOLS
from src.agent.toolsets.agents import UNAVAILABLE, build_agent_tools
from src.config import ceiling
from src.config.levels import SERVICE_MIN_LEVEL, Level
from src.config.loader import load_agent_config, load_user
from src.core.sources.registry import SourceRegistry
from src.terminal.context import run_context

ALL_TOOLS = (*READ_TOOLS, *CHANGE_TOOLS)


class LevelMatrix(AgentsFixture):
    def test_the_group_is_in_every_table_at_operator(self) -> None:
        self.assertIn("agents", TOOL_GROUP_IDS)
        self.assertEqual(SERVICE_MIN_LEVEL["agents"], Level.OPERATOR)
        self.assertEqual(ceiling.GROUP_MIN_LEVEL["agents"], Level.OPERATOR)
        self.assertEqual(set(TOOL_GROUP_TOOLS["agents"]), set(ALL_TOOLS))
        for name in ALL_TOOLS:
            self.assertEqual(ceiling.TOOL_MIN_LEVEL[name], Level.OPERATOR, name)
            self.assertIn(name, DISPOSABLE_BLOCKED_TOOLS)

    def test_tools_exist_from_operator_up_and_not_below(self) -> None:
        for level in Level:
            built = build_agent_tools(resolved("user-a", "chief", level, ["agents"]))
            with self.subTest(level=level.label):
                names = [func.tool_spec["function"]["name"] for func in built]
                self.assertEqual(names, list(ALL_TOOLS) if level >= Level.OPERATOR else [])
                self.assertEqual(ceiling.group_allowed(level, "agents"), level >= Level.OPERATOR)
                self.assertEqual(ceiling.capabilities(level).agents, level >= Level.OPERATOR)
                for name in ALL_TOOLS:
                    self.assertEqual(ceiling.tool_allowed(level, name), level >= Level.OPERATOR)

    def test_l3_is_refused_at_runtime_even_if_a_tool_object_exists(self) -> None:
        # a stored agent that lost the group or the level (the user lowered it mid-chat) is refused by the port itself
        self.write_agent("chief", "id: chief\nname: Chief\nlevel: collaborator\nchat_config:\n  tool_groups: [memory_read, agents]\n")
        text = self.call("agent_list", self.run_of())
        self.assertIn("no longer holds the agents tool group", text)
        self.write_agent("chief", "id: chief\nname: Chief\nlevel: operator\nchat_config:\n  tool_groups: [memory_read]\n")
        self.assertIn("no longer holds the agents tool group", self.call("agent_list", self.run_of()))

    def test_l4_and_l5_are_allowed(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: operator", "max_level: admin"))
        self.write_agent("chief", "id: chief\nname: Chief\nlevel: admin\nchat_config:\n  tool_groups: [memory_read, agents]\n")
        for level in (Level.OPERATOR, Level.ADMIN):
            data = json.loads(self.call("agent_list", self.run_of(), as_level=level))
            self.assertEqual(len(data["agents"]), 5, level)

    def test_an_agent_cannot_hold_the_group_below_operator(self) -> None:
        from src.config.models import AgentConfig
        from src.utils.i18n import UserMessageError

        with self.assertRaises(UserMessageError):
            self.service._agents.create("user-a", AgentConfig.model_validate(
                {"id": "low", "name": "Low", "level": "collaborator", "chat_config": {"tool_groups": ["agents"]}}))


class Reads(AgentsFixture):
    def test_agent_list_has_the_fields_and_asks_nothing(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\ndescription: Helper\nlevel: assistant\nchat_config:\n  model: ollama:m\n  tools: false\n  tool_groups: [memory_read, web]\n")
        run = self.run_of()
        data = json.loads(self.call("agent_list", run))
        by_id = {item["id"]: item for item in data["agents"]}
        self.assertEqual(data["max_level"], "operator")
        self.assertEqual(by_id["bot"], {
            "id": "bot", "name": "Bot", "description": "Helper", "level": "assistant", "model": "ollama:m",
            "tool_groups": ["memory_read", "web"], "enabled": False, "memory": True, "dream_enabled": False, "is_you": False,
        })
        self.assertTrue(by_id["chief"]["is_you"])
        self.assertEqual(run.cards, [])

    def test_agent_get_has_the_soul_and_no_secrets_or_rules(self) -> None:
        self.write_agent("ops", self.read("user-a/agents/ops.yaml") + "approval_rules:\n  rules:\n    - {verdict: deny, command: 'rm *'}\n")
        self.service.update_soul("user-a", "bot", "I am the bot SOUL.")
        run = self.run_of()
        data = json.loads(self.call("agent_get", run, agent_id="bot"))
        self.assertEqual(data["soul"], "I am the bot SOUL.")
        self.assertEqual((data["id"], data["name"], data["level"]), ("bot", "Bot", "assistant"))
        text = self.call("agent_get", run, agent_id="ops")
        ops = json.loads(text)
        self.assertEqual(ops["approval_rules"], 1)  # a count: the rules themselves are the permissions tools'
        self.assertNotIn("rm *", text)
        for secret in ("secret", "api_key", "key_file", "password"):
            self.assertNotIn(secret, text)
        self.assertEqual(run.cards, [])

    def test_agent_templates_lists_every_template_and_whether_it_is_usable(self) -> None:
        data = json.loads(self.call("agent_templates", self.run_of()))
        by_name = {row["template"]: row for row in data["templates"]}
        self.assertEqual(set(by_name), set(templates.templates()))
        self.assertEqual(by_name["guide"]["level"], "operator")
        self.assertEqual(by_name["guide"]["tool_groups"], ["memory_read", "memory_edit", "web", "self", "agents", "permissions"])
        self.assertEqual(by_name["default"]["level"], "assistant")
        self.assertTrue(all(row["available"] for row in data["templates"]))

    def test_a_template_above_the_user_maximum_is_listed_as_unavailable(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: operator", "max_level: collaborator"))
        data = json.loads(self.call("agent_templates", self.run_of()))
        guide = next(row for row in data["templates"] if row["template"] == "guide")
        self.assertFalse(guide["available"])
        self.assertIn("maximum", guide["why_not"])

    def test_reads_never_write(self) -> None:
        before = self.files_of("user-a")
        run = self.run_of()
        for name, args in (("agent_list", {}), ("agent_get", {"agent_id": "boss"}), ("agent_templates", {})):
            self.call(name, run, **args)
        self.assertEqual(before, self.files_of("user-a"))
        self.assertEqual(run.cards, [])


class Ownership(AgentsFixture):
    def test_another_users_agent_is_never_reachable_by_any_tool(self) -> None:
        before = self.files_of("user-b")
        for target in ("stranger", "../user-b/agents/stranger", "user-b/stranger", "..", ""):
            for name, args in (
                ("agent_get", {}), ("agent_update", {"description": "x"}), ("agent_rename", {"new_id": "other"}), ("agent_delete", {}),
            ):
                run = self.run_of()
                with self.subTest(tool=name, target=target):
                    text = self.call(name, run, agent_id=target, **args)
                    self.assertTrue(text.startswith("error: there is no agent"), text)
                    self.assertEqual(run.cards, [])
        self.assertEqual(before, self.files_of("user-b"))
        self.assertTrue((self.config_dir / "user-b" / "agents" / "stranger.yaml").exists())

    def test_no_tool_takes_a_user_or_path_argument(self) -> None:
        for name, func in self.tools().items():
            props = func.tool_spec["function"]["parameters"]["properties"]
            self.assertFalse({"user", "user_id", "path"} & set(props), name)

    def test_agent_list_shows_only_the_callers_user(self) -> None:
        data = json.loads(self.call("agent_list", self.run_of()))
        self.assertNotIn("stranger", {item["id"] for item in data["agents"]})

    def test_a_run_of_another_user_or_agent_is_refused(self) -> None:
        for run in (AgentsRun(self.service, "chief", user="user-b"), AgentsRun(self.service, "boss")):
            self.assertEqual(self.call("agent_list", run), UNAVAILABLE)

    def test_a_created_agent_belongs_to_the_callers_user_only(self) -> None:
        self.ok("agent_create", self.run_of(), agent_id="made", name="Made")
        self.assertIn("made", load_user("user-a").agents)
        self.assertFalse((self.config_dir / "user-b" / "agents" / "made.yaml").exists())


class Cards(AgentsFixture):
    def test_every_mutation_shows_a_once_or_deny_card_and_applies_only_on_once(self) -> None:
        cases = (
            ("agent_create", {"agent_id": "made", "name": "Made"}),
            ("agent_update", {"agent_id": "bot", "description": "New text"}),
            ("agent_rename", {"agent_id": "bot", "new_id": "robot"}),
            ("agent_delete", {"agent_id": "bot"}),
        )
        for name, args in cases:
            before = self.files_of("user-a")
            run = self.run_of(answers=(("deny", "once"),))
            text = self.call(name, run, **args)
            with self.subTest(tool=name, answer="deny"):
                self.assertTrue(text.startswith("error: the user did not approve"), text)
                self.assertIn("Do not retry", text)
                self.assertEqual(len(run.cards), 1)
                self.assertEqual(run.cards[0]["tool"], name)
                self.assertEqual(run.cards[0]["options"], ["once"])
                self.assertEqual(before, self.files_of("user-a"))
        applied = cases[:3] + (("agent_delete", {"agent_id": "ops"}),)
        for name, args in applied:
            run = self.run_of()
            data = json.loads(self.call(name, run, **args))
            with self.subTest(tool=name, answer="once"):
                self.assertEqual(data["status"], "applied")
                self.assertEqual(data["approved_by"], "user")
                self.assertEqual((len(run.cards), run.cards[0]["options"]), (1, ["once"]))
        self.assertEqual(sorted(self.agent_ids()), sorted(["chief", "boss", "robot", "box", "made"]))

    def test_always_is_not_a_scope_and_nothing_is_applied(self) -> None:
        run = self.run_of(answers=(("approve", "always"),))
        text = self.call("agent_delete", run, agent_id="bot")
        self.assertTrue(text.startswith("error:"), text)
        self.assertTrue(run.errors)  # the broker refused the scope the card did not offer
        self.assertIn("bot", self.agent_ids())

    def test_unanswered_card_applies_nothing(self) -> None:
        run = self.run_of(silent=True)
        with patch.object(self.service._approvals, "request", return_value=_Outcome(False, "timeout")):
            text = self.call("agent_delete", run, agent_id="bot")
        self.assertIn("did not approve", text)
        self.assertIn("bot", self.agent_ids())

    def test_the_update_card_is_a_readable_diff(self) -> None:
        self.service.update_soul("user-a", "bot", "line one\nline two\n")
        run = self.run_of()
        self.call("agent_update", run, agent_id="bot", description="Better helper", soul="line one\nline 2\nline three\n",
                  settings={"reasoning_effort": "low"})
        card = run.cards[0]["command"]
        for expected in ("Agent 'Chief' [chief] (level operator)", "agent 'Bot' [bot]", "description:  -> Better helper",
                         "reasoning_effort: medium -> low", "SOUL: 18 -> 27 characters", "-line two", "+line 2", "+line three",
                         "Approving applies exactly this change, once."):
            self.assertIn(expected, card)

    def test_the_create_card_lists_id_template_level_groups_and_soul(self) -> None:
        run = self.run_of(answers=(("deny", "once"),))
        self.call("agent_create", run, agent_id="helper", name="Helper", description="Does things", template="guide")
        card = run.cards[0]["command"]
        for expected in ("CREATE", "id: helper", "template: guide", "level: operator", "agents, permissions",
                         "SOUL: ", "from the template", "holds the agents and permissions group"):
            self.assertIn(expected, card)

    def test_the_rename_and_delete_cards_say_what_is_kept(self) -> None:
        run = self.run_of(answers=(("deny", "once"), ("deny", "once")))
        self.call("agent_rename", run, agent_id="bot", new_id="robot")
        self.call("agent_delete", run, agent_id="bot")
        self.assertIn("id: bot -> robot", run.cards[0]["command"])
        self.assertIn("data folder (memories, SOUL, chats) stays on disk", run.cards[1]["command"])

    def test_an_unchanged_update_asks_nothing(self) -> None:
        run = self.run_of()
        data = json.loads(self.call("agent_update", run, agent_id="bot", name="Bot"))
        self.assertEqual(data["status"], "unchanged")
        self.assertEqual(run.cards, [])

    def test_a_change_made_meanwhile_is_not_applied(self) -> None:
        run = self.run_of()
        original = self.service._approvals.request

        def request(*args, **kwargs):
            self.write_agent("bot", "id: bot\nname: Bot\ndescription: edited by hand\n")  # while the card is open
            return original(*args, **kwargs)

        with patch.object(self.service._approvals, "request", request):
            text = self.call("agent_update", run, agent_id="bot", description="mine")
        self.assertIn("changed while the approval was pending", text)
        self.assertEqual(load_agent_config("user-a", "bot").description, "edited by hand")


class _Outcome:
    def __init__(self, approved: bool, reason: str) -> None:
        self.approved, self.reason, self.scope, self.approval_id = approved, reason, "once", "a1"


class Interactivity(AgentsFixture):
    def test_dreams_tasks_workers_and_missing_ports_refuse(self) -> None:
        for run in (
            AgentsRun(self.service, "chief", interactive=False),
            AgentsRun(self.service, "chief", with_port=False),
        ):
            for name in ALL_TOOLS:
                args = {"agent_id": "bot", "name": "x"} if name == "agent_create" else {"agent_id": "bot", "new_id": "x"} if name == "agent_rename" else {"agent_id": "bot"} if name != "agent_list" and name != "agent_templates" else {}
                with self.subTest(tool=name):
                    self.assertEqual(self.call(name, run, **args), UNAVAILABLE)
        self.assertIn("bot", self.agent_ids())

    def test_a_worker_run_with_a_parent_chat_refuses(self) -> None:
        run = AgentsRun(self.service, "chief")
        context = run.context()
        context.approval_target = ("chief", "parent")
        with run_context(context):
            self.assertEqual(self.tools()["agent_delete"](agent_id="bot"), UNAVAILABLE)
        context = run.context()
        context.task = {"id": "t1"}
        with run_context(context):
            self.assertEqual(self.tools()["agent_list"](), UNAVAILABLE)

    def test_build_tools_offers_them_only_from_operator_and_never_in_a_disposable_chat(self) -> None:
        from src.agent.tools import build_tools

        for level, disposable, expected in (
            (Level.COLLABORATOR, False, False), (Level.OPERATOR, False, True), (Level.ADMIN, False, True), (Level.OPERATOR, True, False),
        ):
            agent = type("A", (), {})()
            agent.config = resolved("user-a", "chief", level, ["agents"])
            specs, handlers = build_tools(agent, None, ["agents"], disposable=disposable)
            with self.subTest(level=level.label, disposable=disposable):
                self.assertEqual(set(ALL_TOOLS) <= set(handlers), expected)
                self.assertEqual(bool(set(ALL_TOOLS) & set(handlers)), expected)

    def test_a_worker_level_drops_the_group(self) -> None:
        config = resolved("user-a", "chief", Level.OPERATOR, ["memory_read", "agents"])
        worker = ceiling.restrict_config(config, Level.COLLABORATOR)
        self.assertNotIn("agents", worker.tool_groups)
        self.assertEqual(build_agent_tools(worker), [])

    def test_a_dream_and_a_disposable_chat_never_get_the_tools(self) -> None:
        for level in Level:
            self.assertFalse(set(ALL_TOOLS) & ceiling.dream_allowed_tools(level), level.label)
        self.assertFalse(ceiling.dream_capabilities(Level.ADMIN).agents)


class HigherLevelAgents(AgentsFixture):
    MAX_LEVEL = "admin"

    def setUp(self) -> None:
        super().setUp()
        self.write_agent("top", "id: top\nname: Top\nlevel: admin\nchat_config:\n  tool_groups: [memory_read]\n")
        self.write_user(self.read("user-a/user.yaml").replace("[chief, boss,", "[chief, top, boss,"))

    def test_an_operator_cannot_change_rename_or_delete_an_admin_agent_but_can_read_it(self) -> None:
        before = self.files_of("user-a")
        for name, args in (("agent_update", {"description": "x"}), ("agent_rename", {"new_id": "up"}), ("agent_delete", {})):
            run = self.run_of()
            with self.subTest(tool=name):
                text = self.call(name, run, agent_id="top", **args)
                self.assertIn("above your own level", text)
                self.assertEqual(run.cards, [])
        self.assertEqual(before, self.files_of("user-a"))
        self.assertEqual(json.loads(self.call("agent_get", self.run_of(), agent_id="top"))["level"], "admin")

    def test_an_admin_agent_can_change_it(self) -> None:
        self.write_agent("chief", "id: chief\nname: Chief\nlevel: admin\nchat_config:\n  tool_groups: [memory_read, agents]\n")
        self.ok("agent_update", self.run_of(), as_level=Level.ADMIN, agent_id="top", description="by admin")
        self.assertEqual(load_agent_config("user-a", "top").description, "by admin")

    def test_create_cannot_exceed_the_callers_level(self) -> None:
        run = self.run_of()
        text = self.call("agent_create", run, agent_id="up", name="Up", level="admin")
        self.assertIn("above your own level 'operator'", text)
        self.assertEqual(run.cards, [])
        self.assertNotIn("up", self.agent_ids())


class SelfProtection(AgentsFixture):
    def test_no_self_delete_no_self_rename_no_self_demotion_by_this_tool(self) -> None:
        before = self.files_of("user-a")
        for name, args in (("agent_delete", {}), ("agent_rename", {"new_id": "me"}), ("agent_update", {"name": "New"}),
                           ("agent_update", {"settings": {"level": "reader"}}), ("agent_update", {"settings": {"enabled": False}}),
                           ("agent_update", {"model": "ollama:x"})):
            run = self.run_of()
            with self.subTest(tool=name, args=args):
                self.assertTrue(self.call(name, run, agent_id="chief", **args).startswith("error:"))
                self.assertEqual(run.cards, [])
        self.assertEqual(before, self.files_of("user-a"))

    def test_a_self_update_of_description_and_soul_is_allowed_after_approval(self) -> None:
        self.service.update_soul("user-a", "chief", "old soul")
        run = self.run_of()
        data = json.loads(self.call("agent_update", run, agent_id="chief", description="I help", soul="new soul"))
        self.assertEqual((data["status"], data["self_change"]), ("applied", True))
        self.assertIn("ITS OWN description or SOUL", run.cards[0]["command"])
        self.assertEqual(load_agent_config("user-a", "chief").description, "I help")
        self.assertEqual(load_agent_config("user-a", "chief").level, Level.OPERATOR)  # untouched
        self.assertEqual(SourceRegistry.for_agent("user-a", "chief", data_dir=self.data_dir).agent_files().soul(), "new soul")

    def test_the_last_agent_is_never_deleted(self) -> None:
        port = self.service._agent_admin
        user = load_user("user-a")
        stored = load_agent_config("user-a", "chief")
        single = user.model_copy(update={"agents": ["bot"]})
        with patch.object(port, "_caller", return_value=(single, stored, Level.OPERATOR)):
            with self.assertRaises(Exception) as caught:
                port.plan_delete("user-a", "chief", Level.OPERATOR, "bot")
        self.assertIn("last agent", str(caught.exception))
        self.assertIn("bot", self.agent_ids())


class CreateChecks(AgentsFixture):
    def stored(self, agent_id: str) -> dict:
        import yaml

        return yaml.safe_load(self.read(f"user-a/agents/{agent_id}.yaml"))

    def test_level_defaults_to_the_lowest_that_covers_the_template(self) -> None:
        data = self.ok("agent_create", self.run_of(), agent_id="made", name="Made", description="A plain one")
        self.assertEqual(data["after"]["level"], "assistant")  # the default groups (memory, web, self) need assistant
        config = load_agent_config("user-a", "made")
        self.assertEqual((config.name, config.description, config.level), ("Made", "A plain one", Level.ASSISTANT))
        self.assertIn("made", load_user("user-a").agents)
        self.assertEqual(SourceRegistry.for_agent("user-a", "made", data_dir=self.data_dir).agent_files().soul(), templates.templates()["default"].soul)

    def test_an_explicit_lower_level_works_and_one_below_what_the_config_needs_does_not(self) -> None:
        self.ok("agent_create", self.run_of(), agent_id="low", name="Low", level="assistant")
        text = self.call("agent_create", self.run_of(), agent_id="lower", name="Lower", level="reader")
        self.assertIn("needs at least 'assistant'", text)
        self.assertNotIn("lower", self.agent_ids())

    def test_create_cannot_exceed_the_users_maximum(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("max_level: operator", "max_level: collaborator"))
        run = self.run_of()
        for kwargs in ({"level": "operator"}, {"template": "guide"}):
            text = self.call("agent_create", run, agent_id="up", name="Up", **kwargs)
            with self.subTest(kwargs=kwargs):
                self.assertIn("maximum", text)
        self.assertEqual(run.cards, [])
        self.assertNotIn("up", self.agent_ids())

    def test_create_from_the_guide_template(self) -> None:
        data = self.ok("agent_create", self.run_of(), agent_id="guide", name="", template="guide")
        self.assertEqual((data["after"]["level"], data["after"]["template"]), ("operator", "guide"))
        config = load_agent_config("user-a", "guide")
        self.assertEqual(config.name, "Guide")
        self.assertEqual(config.chat_config.tool_groups, ["memory_read", "memory_edit", "web", "self", "agents", "permissions"])
        self.assertIsNone(config.terminal)
        self.assertEqual(config.level, Level.OPERATOR)
        self.assertIn("setup guide", SourceRegistry.for_agent("user-a", "guide", data_dir=self.data_dir).agent_files().soul())
        self.assertNotIn("terminal_backends:\n  guide", self.read("user-a/user.yaml"))

    def test_create_with_a_soul_and_a_model(self) -> None:
        with patch.object(type(self.service), "_check_agent_model", lambda s, user, config, current: config):
            data = self.ok("agent_create", self.run_of(), agent_id="mine", name="Mine", soul="# SOUL\nI am mine.", model="ollama:m1")
        self.assertEqual(data["after"]["model"], "ollama:m1")
        self.assertEqual(load_agent_config("user-a", "mine").chat_config.model, "ollama:m1")
        self.assertEqual(SourceRegistry.for_agent("user-a", "mine", data_dir=self.data_dir).agent_files().soul(), "# SOUL\nI am mine.")

    def test_invalid_input_is_refused_before_any_card(self) -> None:
        from src.config.settings import memory_limits

        cases = (
            {"agent_id": "bad id", "name": "X"}, {"agent_id": "_user", "name": "X"}, {"agent_id": "../x", "name": "X"},
            {"agent_id": "bot", "name": "X"},                               # taken
            {"agent_id": "x", "name": "a\nb"},
            {"agent_id": "x", "name": "n" * 101}, {"agent_id": "x", "name": "X", "description": "d" * 501},
            {"agent_id": "x", "name": "X", "description": "two\nlines"},
            {"agent_id": "x", "name": "X", "template": "nope"},
            {"agent_id": "x", "name": "X", "soul": "s" * (memory_limits().soul_chars + 1)},
            {"agent_id": "x", "name": "X", "level": "wizard"},
        )
        run = self.run_of()
        for args in cases:
            with self.subTest(args={key: str(value)[:20] for key, value in args.items()}):
                self.assertTrue(self.call("agent_create", run, **args).startswith("error:"))
        self.assertEqual(run.cards, [])
        self.assertEqual(self.agent_ids(), ["chief", "boss", "bot", "ops", "box"])

    def test_a_template_beyond_the_callers_backends_is_refused(self) -> None:
        from src.agent.templates import AgentTemplate

        local = AgentTemplate("localbox", "x", {"level": "operator", "chat_config": {"tool_groups": ["terminal"]},
                                                  "terminal": {"backends": {"local": {"approvals": "manual"}}}}, "soul")
        for template, expected in ((local, "terminal backend 'local'"),):
            with patch("src.agent.templates.templates", return_value={**templates.templates(), template.name: template}):
                run = self.run_of()
                text = self.call("agent_create", run, agent_id="x", name="X", template=template.name)
                with self.subTest(template=template.name):
                    self.assertIn(expected, text)
                    self.assertEqual(run.cards, [])

    def test_the_new_agent_gets_the_agents_group_only_when_the_template_says_and_a_human_approves(self) -> None:
        run = self.run_of(answers=(("deny", "once"),))
        self.call("agent_create", run, agent_id="guide", name="G", template="guide")
        self.assertNotIn("guide", self.agent_ids())
        self.assertEqual(len(run.cards), 1)
        data = self.ok("agent_create", self.run_of(), agent_id="plain", name="Plain")
        self.assertNotIn("agents", data["after"]["tool_groups"])
        self.assertNotIn("permissions", data["after"]["tool_groups"])


class UpdateChecks(AgentsFixture):
    def test_plain_fields_and_settings_apply(self) -> None:
        with patch.object(type(self.service), "_check_agent_model", lambda s, user, config, current: config):
            self.ok("agent_update", self.run_of(), agent_id="bot", name="Robot", description="Fast", model="ollama:m2",
                    soul="a soul", settings={"enabled": False, "reasoning_effort": "high", "avatar_style": "free", "dream_enabled": True})
        config = load_agent_config("user-a", "bot")
        self.assertEqual((config.name, config.description, config.chat_config.model), ("Robot", "Fast", "ollama:m2"))
        self.assertEqual((config.chat_config.tools, config.chat_config.reasoning_effort, config.avatar_style, config.dream.enabled),
                         (False, "high", "free", True))
        self.assertEqual(config.level, Level.ASSISTANT)  # permission fields are untouched
        self.assertEqual(SourceRegistry.for_agent("user-a", "bot", data_dir=self.data_dir).agent_files().soul(), "a soul")

    def test_permission_fields_are_rejected_with_a_pointer_to_the_permissions_tools(self) -> None:
        before = self.files_of("user-a")
        for settings in ({"level": "operator"}, {"tool_groups": ["web"]}, {"groups": ["web"]}, {"approval_rules": {"rules": []}},
                         {"allow": ["x"]}, {"deny": ["x"]}):
            run = self.run_of()
            with self.subTest(settings=settings):
                text = self.call("agent_update", run, agent_id="bot", settings=settings)
                self.assertTrue(text.startswith("error:"), text)
                self.assertIn("permissions tools", text)
                self.assertIn("agent_set_level", text)
                self.assertEqual(run.cards, [])
        for args in ({"level": "admin"}, {"tool_groups": ["web"]}, {"approval_rules": {"default": "allow"}}):  # as top-level arguments
            run = self.run_of()
            with self.subTest(args=args):
                text = self.call("agent_update", run, agent_id="bot", **args)
                self.assertIn("permissions tools", text)
                self.assertEqual(run.cards, [])
        self.assertEqual(before, self.files_of("user-a"))

    def test_access_blocks_are_the_users_and_unknown_settings_are_refused(self) -> None:
        for settings, expected in (({"terminal": {}}, "only the user"), ({"mcp": {}}, "only the user"), ({"folders": []}, "only the user"),
                                   ({"delegation": {}}, "only the user"), ({"id": "x"}, "only the user"), ({"bogus": 1}, "unknown setting")):
            with self.subTest(settings=settings):
                self.assertIn(expected, self.call("agent_update", self.run_of(), agent_id="bot", settings=settings))

    def test_invalid_values_and_sizes_are_refused(self) -> None:
        for args in ({"name": ""}, {"name": "n" * 101}, {"description": "d" * 501}, {"soul": ""}, {"soul": "s" * 100000},
                     {"settings": {"reasoning_tokens": "many"}}, {"settings": "x"}):
            run = self.run_of()
            with self.subTest(args={key: str(value)[:15] for key, value in args.items()}):
                self.assertTrue(self.call("agent_update", run, agent_id="bot", **args).startswith("error:"))
                self.assertEqual(run.cards, [])

    def test_an_update_keeps_level_and_rules_of_the_target(self) -> None:
        self.write_agent("ops", self.read("user-a/agents/ops.yaml") + "approval_rules:\n  rules:\n    - {verdict: deny, command: 'rm *'}\n")
        self.ok("agent_update", self.run_of(), agent_id="ops", description="Keeps rules")
        config = load_agent_config("user-a", "ops")
        self.assertEqual((config.level, len(config.approval_rules.rules)), (Level.OPERATOR, 1))

    def test_a_busy_agent_is_reported_and_nothing_changes(self) -> None:
        slot = self.service._slot("user-a", "bot")
        with slot.state:
            slot.pending["chat"] = [object()]
        text = self.call("agent_update", self.run_of(), agent_id="bot", description="busy")
        self.assertIn("error:", text)
        self.assertIn("Nothing was changed", text)
        with slot.state:
            slot.pending.clear()


class RenameDelete(AgentsFixture):
    def test_rename_moves_the_data_the_avatar_and_the_references(self) -> None:
        self.service.update_soul("user-a", "bot", "soul stays")
        from src.utils.avatars import AvatarStore

        AvatarStore(self.data_dir).save("user-a", "bot", PNG)
        self.service._preferences.update("user-a", __import__("src.config.preferences", fromlist=["UserPreferences"]).UserPreferences(default_agent_id="bot"))
        data = self.ok("agent_rename", self.run_of(), agent_id="bot", new_id="robot")
        self.assertEqual((data["before"], data["after"]), ({"id": "bot"}, {"id": "robot"}))
        self.assertNotIn("bot", self.agent_ids())
        self.assertIn("robot", self.agent_ids())
        self.assertEqual(load_agent_config("user-a", "robot").name, "Bot")  # the display name stays
        self.assertFalse((self.data_dir / "user-a" / "bot").exists())
        self.assertEqual(SourceRegistry.for_agent("user-a", "robot", data_dir=self.data_dir).agent_files().soul(), "soul stays")
        self.assertIsNone(AvatarStore(self.data_dir).get("user-a", "bot"))
        self.assertEqual(AvatarStore(self.data_dir).read(AvatarStore(self.data_dir).get("user-a", "robot")), PNG)
        self.assertEqual(self.service._preferences.get("user-a").default_agent_id, "robot")

    def test_rename_refuses_bad_ids_and_taken_ids(self) -> None:
        for new_id in ("bad id", "x/../y", "", "boss", "_user", "n" * 65):
            run = self.run_of()
            with self.subTest(new_id=new_id):
                self.assertTrue(self.call("agent_rename", run, agent_id="bot", new_id=new_id).startswith("error:"))
                self.assertEqual(run.cards, [])
        self.assertIn("bot", self.agent_ids())

    def test_delete_removes_the_configuration_and_keeps_the_data_like_the_web(self) -> None:
        self.service.update_soul("user-a", "bot", "kept")
        self.ok("agent_delete", self.run_of(), agent_id="bot")
        self.assertNotIn("bot", self.agent_ids())
        self.assertFalse((self.config_dir / "user-a" / "agents" / "bot.yaml").exists())
        self.assertTrue((self.data_dir / "user-a" / "bot").is_dir())  # the data folder stays on disk
        self.assertIn("bot", self.audit_names())

    def audit_names(self) -> str:
        return json.dumps(self.audit_lines("chief"))


class Audit(AgentsFixture):
    def test_every_attempt_is_audited_approved_denied_or_refused(self) -> None:
        self.ok("agent_update", self.run_of(), agent_id="bot", description="one")
        self.call("agent_update", self.run_of(answers=(("deny", "once"),)), agent_id="bot", description="two")
        self.call("agent_delete", self.run_of(), agent_id="chief")  # refused before any card
        lines = [line for line in self.audit_lines("chief") if line["event"] == "permission_change"]
        by_decision = {line["decision"]: line for line in lines if line["target"] != "chief"} | {"refused": lines[-1]}
        self.assertEqual([line["decision"] for line in lines], ["approved", "denied", "refused"])
        approved = by_decision["approved"]
        self.assertEqual((approved["tool"], approved["field"], approved["action"], approved["actor"], approved["target"], approved["applied"]),
                         ("agent_update", "agent", "update", "chief", "bot", True))
        self.assertEqual(approved["after"], {"description": "one"})
        self.assertEqual(lines[2]["action"], "refused")
        bot_lines = self.audit_lines("bot")  # the target's own file gets the applied change too
        self.assertEqual([line["decision"] for line in bot_lines], ["approved"])

    def test_the_audit_never_holds_the_soul_text(self) -> None:
        self.ok("agent_update", self.run_of(), agent_id="bot", soul="very private soul text")
        self.assertNotIn("very private soul text", json.dumps(self.audit_lines("chief")))
        line = self.audit_lines("chief")[-1]
        self.assertEqual(line["after"]["soul"]["chars"], len("very private soul text"))

    def test_an_unwritable_audit_trail_blocks_the_change(self) -> None:
        with patch("src.service.agent_admin.audit_writable", return_value=False):
            run = self.run_of()
            text = self.call("agent_update", run, agent_id="bot", description="x")
        self.assertIn("audit trail cannot be written", text)
        self.assertEqual(run.cards, [])

    def test_created_agents_get_their_own_audit_line(self) -> None:
        self.ok("agent_create", self.run_of(), agent_id="made", name="Made")
        self.assertEqual([line["decision"] for line in self.audit_lines("made")], ["approved"])


if __name__ == "__main__":
    unittest.main()
