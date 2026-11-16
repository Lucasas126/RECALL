"""Level ceilings at run time: tool building, dispatch, MCP, workers, delegation targets, dreams and templates."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch


from tests.support.admin_helpers import AdminFixture
from tests.support.terminal_helpers import ssh_config
from tests.agent.dream.test_dream import DreamFixture, ScriptedProvider, _call
from tests.providers.test_remember_mcp_hardening import _manager, _names
from tests.tasks.test_subagents import LIMITS
from tests.tasks.test_subagents_agents import AgentTargetFixture

from src.agent.agent import Agent
from src.agent.dream import DreamRunner, build_dream_tools
from src.agent.tool_groups import TOOL_GROUP_TOOLS
from src.agent.tools import build_tools
from src.agent.toolsets.delegate import TaskError, build_delegate_tools
from src.agent.toolsets.mcp import build_mcp_tools
from src.config.ceiling import dream_allowed_tools, restrict_config
from src.config.levels import Level
from src.config.models import (
    AgentDelegationConfig, AgentMcpConfig, AgentTerminalBackend, AgentTerminalConfig, DelegationTarget, DreamConfig,
    LocalBackendConfig, ModelsConfig, ResolvedAgent,
)
from src.core.review import ReviewQueue
from src.core.sources.conversations import ChatTask, ConversationsSource
from src.core.sources.registry import SourceRegistry
from src.providers.provider import LLMProvider

L = Level
ALL_GROUPS = ["memory_read", "memory_edit", "web", "self", "folders_read", "folders_edit", "terminal", "delegate"]
BOX, ROOT, LOCAL = ssh_config(), ssh_config(user="root"), LocalBackendConfig(type="local", workdir="/app")


def resolved(level: L | None, groups=None, **changes) -> ResolvedAgent:
    fields = dict(
        user_id="u", id="a", name="A", models=ModelsConfig(), provider_name="ollama", api_key="",
        embeddings_provider_name="ollama", embeddings_api_key="", tools=True, tool_groups=groups,
    )
    return ResolvedAgent(**{**fields, **changes}, level=level)


def with_terminal(level: L | None, names: tuple[str, ...], groups=("memory_read", "terminal")) -> ResolvedAgent:
    available = {"box": BOX, "root": ROOT, "local": LOCAL}
    return resolved(
        level, list(groups),
        terminal=AgentTerminalConfig(
            backends={name: AgentTerminalBackend() for name in names}, default_backend=names[0],
        ),
        terminal_backends={name: available[name] for name in names},
    )


def host_of(config: ResolvedAgent) -> SimpleNamespace:
    sources = Mock()
    sources.folders.return_value = []
    return SimpleNamespace(sources=sources, config=config, index=None)


class ToolBuildingTests(unittest.TestCase):
    def names(self, config: ResolvedAgent, requested=None) -> set[str]:
        specs, handlers = build_tools(host_of(config), Mock(), requested or config.tool_groups)
        self.assertEqual({s["function"]["name"] for s in specs}, set(handlers))
        return set(handlers)

    def test_every_level_builds_only_the_groups_it_allows(self) -> None:
        first = {"memory_read": L.READER, "memory_edit": L.ASSISTANT, "self": L.ASSISTANT, "web": L.ASSISTANT}
        for level in L:
            names = self.names(resolved(level, ALL_GROUPS))
            with self.subTest(level=level.label):
                for group, minimum in first.items():
                    tools = set(TOOL_GROUP_TOOLS[group]) - {"skill_view"}
                    self.assertEqual(tools <= names, level >= minimum, group)
                self.assertEqual("task_start" in names, level >= L.COLLABORATOR)
                self.assertNotIn("terminal", names)  # no terminal block: nothing to build

    def test_a_group_above_the_level_is_dropped_even_when_requested_and_configured(self) -> None:
        names = self.names(resolved(L.READER, ["memory_read", "memory_edit", "web", "self"]))
        self.assertIn("search", names)
        self.assertFalse(names & {"write", "edit", "memory", "soul_edit", "web_search"})
        self.assertEqual(self.names(resolved(L.MINIMAL, ["memory_read"])), set())

    def test_skill_view_is_never_gated(self) -> None:
        specs, handlers = build_tools(host_of(resolved(L.MINIMAL, [])), None, [], skill_view=True)
        self.assertEqual(set(handlers), {"skill_view"})

    def test_terminal_needs_operator_and_backends_follow_their_class(self) -> None:
        cases = {
            (L.COLLABORATOR, ("box",)): False,
            (L.OPERATOR, ("box",)): True,
            (L.OPERATOR, ("root",)): False,     # root ssh is admin-only
            (L.OPERATOR, ("local",)): False,    # the local backend is admin-only
            (L.OPERATOR, ("local", "root", "box")): True,   # only the unprivileged one survives
            (L.ADMIN, ("root",)): True,
            (L.ADMIN, ("local",)): True,
        }
        for (level, backends), expected in cases.items():
            with self.subTest(level=level.label, backends=backends):
                names = self.names(with_terminal(level, backends))
                self.assertEqual("terminal" in names, expected)
                self.assertEqual("sandbox_write" in names, expected)

    def test_restrict_config_trims_backends_and_default(self) -> None:
        config = with_terminal(L.ADMIN, ("local", "root", "box"))
        operator = restrict_config(config, L.OPERATOR)
        self.assertEqual(set(operator.terminal_backends), {"box"})
        self.assertEqual((set(operator.terminal.backends), operator.terminal.default_backend), ({"box"}, "box"))
        self.assertIs(operator.level, L.OPERATOR)
        self.assertEqual(len(config.terminal_backends), 3)  # the original is untouched
        self.assertIsNone(restrict_config(config, L.COLLABORATOR).terminal)
        self.assertIs(restrict_config(config, L.ADMIN), config)
        self.assertIs(restrict_config(config, L.ADMIN).level, L.ADMIN)

    def test_restrict_never_raises_a_level(self) -> None:
        self.assertIs(restrict_config(resolved(L.READER, ["memory_read"]), L.ADMIN).level, L.READER)

    def test_delegation_tools_follow_the_level(self) -> None:
        delegation = AgentDelegationConfig(targets=[DelegationTarget(agent="other")])
        config = resolved(L.ASSISTANT, ["delegate", "web"], delegation=delegation)
        self.assertEqual(build_delegate_tools(config), [])  # workers need collaborator
        collaborator = build_delegate_tools(config.model_copy(update={"level": L.COLLABORATOR}))
        props = collaborator[0].tool_spec["function"]["parameters"]["properties"]
        self.assertNotIn("agent", props)  # targets need operator
        operator = build_delegate_tools(config.model_copy(update={"level": L.OPERATOR}))
        self.assertEqual(operator[0].tool_spec["function"]["parameters"]["properties"]["agent"]["enum"], ["", "other"])


def make_agent(config: ResolvedAgent, conversations: ConversationsSource | None = None, chat_id: str | None = None) -> Agent:
    registry = Mock()
    if conversations is not None:
        registry.conversations.return_value = conversations
    provider = Mock(spec=LLMProvider)
    provider.context_window.return_value = 10000
    with (
        patch("src.agent.agent.SourceRegistry.for_agent", return_value=registry),
        patch("src.agent.agent.VectorIndex.for_agent"),
        patch("src.agent.agent.TavilyClient.from_config", return_value=Mock()),
    ):
        return Agent(config, provider, chat_id)


class DispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conversations = ConversationsSource(Path(directory.name))

    def agent(self, level: L) -> Agent:
        return make_agent(resolved(level, [], associative_memory="off"), self.conversations)

    def run_call(self, agent: Agent, name: str, handler: Mock) -> list[dict]:
        agent._tool_handlers = {name: handler}
        return agent._run_tools([{"id": "c1", "name": name, "arguments": {}}])

    def test_a_tool_above_the_level_is_refused_even_if_a_handler_exists(self) -> None:
        cases = {
            "write": L.ASSISTANT, "folder_write": L.COLLABORATOR, "task_start": L.COLLABORATOR,
            "terminal": L.OPERATOR, "sandbox_patch": L.OPERATOR, "web_search": L.ASSISTANT, "search": L.READER,
        }
        for name, first in cases.items():
            for level in L:
                handler = Mock(return_value="ran")
                with self.subTest(tool=name, level=level.label):
                    action = self.run_call(self.agent(level), name, handler)[0]
                    if level >= first:
                        self.assertEqual((action["error"], action["result"]), (False, "ran"))
                        handler.assert_called_once()
                    else:
                        self.assertTrue(action["error"])
                        self.assertIn(f"'{name}' is not allowed", action["result"])
                        self.assertIn(f"'{level.label}'", action["result"])
                        self.assertIn(f"'{first.label}'", action["result"])
                        handler.assert_not_called()

    def test_a_name_outside_the_catalogue_is_not_run(self) -> None:
        handler = Mock(return_value="ran")
        action = self.run_call(self.agent(L.ADMIN), "rm_rf", handler)[0]
        self.assertTrue(action["error"])
        handler.assert_not_called()

    def test_mcp_tools_need_collaborator_at_dispatch(self) -> None:
        for level in (L.ASSISTANT, L.COLLABORATOR):
            agent = self.agent(level)
            agent._mcp_tool_names = frozenset({"fake__a"})
            handler = Mock(return_value="ran")
            agent._tool_handlers = {"fake__a": handler}
            action = agent._run_tools([{"id": "c1", "name": "fake__a", "arguments": {}}])[0]
            with self.subTest(level=level.label):
                self.assertEqual(action["error"], level < L.COLLABORATOR)
                self.assertEqual(handler.called, level >= L.COLLABORATOR)


class McpTests(unittest.TestCase):
    def test_no_mcp_tools_and_no_note_below_collaborator(self) -> None:
        for level in L:
            toolset = build_mcp_tools(_manager("a", "b"), "u", {"fake": AgentMcpConfig()}, level=level)
            with self.subTest(level=level.label):
                if level >= L.COLLABORATOR:
                    self.assertEqual(_names(toolset), ["fake__a", "fake__b"])
                else:
                    self.assertEqual((toolset.specs, toolset.handlers, toolset.notes), ([], {}, []))

    def test_the_per_turn_refresh_applies_the_level(self) -> None:
        for level in (L.ASSISTANT, L.COLLABORATOR):
            config = resolved(level, ["memory_read"], mcp={"fake": AgentMcpConfig()}, associative_memory="off")
            directory = TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            agent = make_agent(config, ConversationsSource(Path(directory.name)))
            agent._mcp = _manager("a")
            agent._refresh_mcp_tools()
            with self.subTest(level=level.label):
                self.assertEqual("fake__a" in agent._tool_handlers, level >= L.COLLABORATOR)
                self.assertEqual("fake__a" in agent._mcp_tool_names, level >= L.COLLABORATOR)


class WorkerAgentTests(unittest.TestCase):
    def worker(self, config: ResolvedAgent, **mark) -> Agent:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        conversations = ConversationsSource(Path(directory.name))
        task = ChatTask(task_id="0123456789ab", parent_agent_id="a", parent_chat_id="p", **mark)
        chat_id = conversations.create(origin="task", task=task)
        return make_agent(config, conversations, chat_id)

    def test_a_worker_never_outranks_its_launcher(self) -> None:
        config = with_terminal(L.ADMIN, ("local", "box"), groups=("web", "terminal"))
        for cap, expected_level, terminal, backends in (
            (L.ASSISTANT, L.ASSISTANT, False, set()),
            (L.OPERATOR, L.OPERATOR, True, {"box"}),   # the local backend is admin-only
            (L.ADMIN, L.ADMIN, True, {"local", "box"}),
            (None, L.ADMIN, True, {"local", "box"}),
        ):
            agent = self.worker(config, groups=["web", "terminal"], backends=["local", "box"], level=cap)
            with self.subTest(cap=getattr(cap, "label", None)):
                self.assertIs(agent.config.level, expected_level)
                self.assertEqual("terminal" in agent._tool_handlers, terminal)
                self.assertEqual(set(agent.config.terminal_backends), backends)
                self.assertEqual("web_search" in agent._tool_handlers, cap is None or cap >= L.ASSISTANT)

    def test_a_worker_of_a_lower_level_refuses_the_tools_of_the_parent(self) -> None:
        config = with_terminal(L.ADMIN, ("box",), groups=("web", "terminal"))
        agent = self.worker(config, groups=["web", "terminal"], backends=["box"], level=L.COLLABORATOR)
        handler = Mock(return_value="ran")
        agent._tool_handlers = {"terminal": handler}
        action = agent._run_tools([{"id": "c1", "name": "terminal", "arguments": {}}])[0]
        self.assertTrue(action["error"])
        handler.assert_not_called()

    def test_the_task_mark_stores_the_level(self) -> None:
        self.assertIs(ChatTask(task_id="x", parent_agent_id="a", parent_chat_id="p", level="Operator").level, L.OPERATOR)
        self.assertIsNone(ChatTask(task_id="x", parent_agent_id="a", parent_chat_id="p").level)


class LauncherLevelTests(AgentTargetFixture):
    """Launch rules: workers need collaborator, other agents as targets need operator, the worker is the minimum."""

    def mark_of(self, live) -> ChatTask:
        record = self.record(live.task_id)
        owner = record["child"]["agent_id"]
        return self.sources[("user-one", owner)].task_mark(record["child"]["chat_id"])

    def test_a_launcher_below_collaborator_cannot_launch_at_all(self) -> None:
        for level in (L.MINIMAL, L.READER, L.ASSISTANT):
            with self.subTest(level=level.label), self.assertRaises(TaskError) as caught:
                self.launch(launcher_level=level)
            self.assertIn("collaborator", str(caught.exception))
        self.assertEqual(self.manager.store.list("user-one"), [])

    def test_an_ephemeral_worker_has_the_level_of_its_launcher(self) -> None:
        for level in (L.COLLABORATOR, L.OPERATOR):
            live = self.launch(launcher_level=level)
            self.finish(live)
            with self.subTest(level=level.label):
                self.assertIs(self.mark_of(live).level, level)

    def test_a_launcher_below_operator_cannot_use_a_target(self) -> None:
        for level in (L.COLLABORATOR,):
            with self.subTest(level=level.label), self.assertRaises(TaskError) as caught:
                self.to_agent(launcher_level=level)
            self.assertIn("operator", str(caught.exception))
        self.assertEqual(self.manager.store.list("user-one"), [])

    def test_a_target_runs_at_the_lower_of_its_own_level_and_the_launchers(self) -> None:
        # agent-two holds web, memory_read, memory_edit and self: assistant
        for launcher in (L.OPERATOR, L.ADMIN):
            live = self.to_agent(launcher_level=launcher)
            self.finish(live)
            with self.subTest(launcher=launcher.label):
                self.assertIs(self.mark_of(live).level, L.ASSISTANT)
        target = self.agents[("user-one", "agent-two")]
        self.assertIs(target.config.level, L.ASSISTANT)

    def test_the_stored_configuration_of_the_launcher_is_used_without_an_explicit_level(self) -> None:
        live = self.to_agent()  # agent-one holds delegate + targets: operator
        self.finish(live)
        self.assertIs(self.mark_of(live).level, L.ASSISTANT)

    def test_groups_the_launcher_level_does_not_cover_cannot_be_granted(self) -> None:
        with self.assertRaises(TaskError) as caught:
            self.launch(launcher_level=L.COLLABORATOR, groups=["terminal"], grantable=["web", "terminal"])
        self.assertIn("level", str(caught.exception))


class DreamLevelTests(DreamFixture):
    def host(self, level: L, groups, **dream) -> SimpleNamespace:
        config = resolved(level, groups, dream=DreamConfig(**dream), associative_memory="off")
        sources = SourceRegistry.for_agent("u", "a", data_dir=self.root)
        return SimpleNamespace(
            config=config, sources=sources, query_memory=lambda *a, **k: [],
            queue=ReviewQueue(sources.conversations(), sources.dream_state()),
        )

    def tools(self, level: L, groups=("memory_read", "memory_edit", "self", "web", "folders_read", "terminal"), **dream) -> set[str]:
        host = self.host(level, list(groups), **{"tools": ["web", "folders"], **dream})
        _, handlers = build_dream_tools(host, host.queue, {}, [], Mock())
        return set(handlers)

    def test_dream_tools_per_level(self) -> None:
        review = {"dream_pending", "dream_read_chat", "dream_mark_reviewed"}
        for level in L:
            names = self.tools(level)
            with self.subTest(level=level.label):
                self.assertTrue(review <= names)
                self.assertEqual(bool(names & {"search", "read", "query_memory"}), level >= L.READER)
                self.assertEqual(bool(names & {"write", "append", "edit"}), level >= L.ASSISTANT)
                self.assertEqual(bool(names & {"memory", "soul_edit", "skill_manage"}), level >= L.ASSISTANT)
                self.assertEqual(bool(names & {"web_search", "web_retrieve"}), level >= L.ASSISTANT)
                self.assertFalse(names & set(TOOL_GROUP_TOOLS["terminal"]))
                self.assertFalse(names & set(TOOL_GROUP_TOOLS["delegate"]))
                self.assertFalse(names & {"folder_write", "folder_edit", "folder_delete", "folder_move", "folder_append"})
                self.assertLessEqual(names, dream_allowed_tools(level))

    def test_the_chat_groups_and_dream_tools_still_narrow_the_level(self) -> None:
        names = self.tools(L.ADMIN, groups=("memory_read",), tools=[])
        self.assertTrue({"search", "dream_pending"} <= names)
        self.assertFalse(names & {"write", "memory", "web_search", "folder_list"})

    def test_a_disallowed_call_is_refused_at_dispatch_even_with_a_handler(self) -> None:
        handler = Mock(return_value="ran")
        for level, expected_error in ((L.READER, True), (L.ASSISTANT, False)):
            handler.reset_mock()
            output, failed = DreamRunner._call({"write": handler}, "write", {}, level)
            with self.subTest(level=level.label):
                self.assertEqual(failed, expected_error)
                self.assertEqual(handler.called, not expected_error)
                if expected_error:
                    self.assertIn("not allowed in a dream", output)
        output, failed = DreamRunner._call({"terminal": handler}, "terminal", {}, L.ADMIN)
        self.assertTrue(failed)
        output, failed = DreamRunner._call({"task_start": handler}, "task_start", {}, L.ADMIN)
        self.assertTrue(failed)

    def test_a_dream_session_of_a_reader_cannot_write_and_a_minimal_agent_does_not_dream(self) -> None:
        self.chat("hello", "hi")
        for level, writes in ((L.READER, False), (L.ASSISTANT, True)):
            provider = ScriptedProvider([
                {"tool_calls": [_call("c1", "append", memory_path="mind_palace:journal/x.md", content="- note")]},
                {"response": "done"},
            ])
            config = resolved(level, ["memory_read", "memory_edit"], dream=DreamConfig(), associative_memory="off",
                              models=ModelsConfig(chat="vendor/chat"))
            runner = DreamRunner(config, provider, self.sources, session_id=f"2026110{1 if writes else 2}-040000-abcd", trigger="manual")
            summary = runner.run()
            with self.subTest(level=level.label):
                self.assertEqual(summary["status"], "completed")
                self.assertEqual(summary["errors"], 0 if writes else 1)
        config = resolved(L.MINIMAL, [], dream=DreamConfig(), models=ModelsConfig(chat="vendor/chat"))
        summary = DreamRunner(config, ScriptedProvider([]), self.sources, session_id="20261103-040000-abcd", trigger="manual").run()
        self.assertEqual(summary["status"], "failed")
        self.assertIn("level reader", summary["error"])


class GuideTemplateLevelTests(AdminFixture):
    def create(self, max_level: str):
        self.write_user(re.sub(r"max_level: \w+", f"max_level: {max_level}", self.read("user-a/user.yaml")))
        return self.run_cli(["agent", "create", "--user", "user-a", "--id", "guide", "--from-template", "guide"])

    def test_guide_is_refused_below_operator_and_writes_nothing(self) -> None:
        for level in ("minimal", "reader", "assistant", "collaborator"):
            code, _, err = self.create(level)
            with self.subTest(level=level):
                self.assertEqual(code, 1)
                self.assertIn(f"'{level}'", err)
                self.assertIn("'operator'", err)
                self.assertNotIn("terminal_backends", self.read("user-a/user.yaml"))
                self.assertFalse((self.config_dir / "user-a" / "agents" / "guide.yaml").exists())

    def test_guide_is_created_for_an_operator_and_an_admin(self) -> None:
        for level in ("operator", "admin"):
            code, out, err = self.create(level)
            with self.subTest(level=level):
                self.assertEqual((code, err), (0, ""), out)
                self.assertTrue((self.config_dir / "user-a" / "agents" / "guide.yaml").exists())
                (self.config_dir / "user-a" / "agents" / "guide.yaml").unlink()
                self.write_user(self.read("user-a/user.yaml").replace("agents: [bot, guide]", "agents: [bot]"))


if __name__ == "__main__":
    unittest.main()
