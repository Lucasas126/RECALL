"""Hardening: remember-target memory grants follow the launcher; MCP tools by class in disposable chats, allow/deny, names."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent))

from test_subagents import LIMITS
from test_subagents_agents import REMEMBER, AgentTargetFixture

from src.agent.agent import Agent
from src.agent.toolsets.delegate import TaskError
from src.agent.toolsets.mcp import McpToolNameError, build_mcp_tools
from src.config.levels import Level
from src.config.models import AgentMcpConfig, ModelsConfig, ResolvedAgent
from src.core.sources.registry import SourceRegistry
from src.providers.mcp import McpTool
from src.providers.provider import LLMProvider

MEMORY = {"memory_read", "memory_edit", "self"}


class RememberGrantTests(AgentTargetFixture):
    def _granted(self, parent_groups: list[str]) -> list[str]:
        type(self).groups = parent_groups
        self.addCleanup(lambda: type(self).__dict__.get("groups") and delattr(type(self), "groups"))
        record = self.finish(self.to_agent(targets=[REMEMBER]))
        return record["grant"]["groups"]

    def test_a_launcher_without_memory_groups_gives_none_to_a_remember_target(self) -> None:
        self.assertFalse(MEMORY & set(self._granted(["delegate", "web"])))

    def test_only_the_memory_groups_the_launcher_holds_are_included(self) -> None:
        granted = self._granted(["delegate", "web", "memory_read"])
        self.assertEqual(set(granted) & MEMORY, {"memory_read"})
        tools = set(self.agents[("user-one", "agent-two")]._tool_handlers)
        self.assertNotIn("write", tools)
        self.assertIn("query_memory", tools)

    def test_a_launcher_holding_all_gets_the_old_behaviour(self) -> None:
        self.assertEqual(set(self._granted(["delegate", "web", *MEMORY])) & MEMORY, MEMORY)

    def test_send_is_refused_once_the_launcher_lost_a_granted_memory_group(self) -> None:
        type(self).groups = ["delegate", "web", *MEMORY]
        self.addCleanup(lambda: delattr(type(self), "groups"))
        record = self.finish(self.to_agent(targets=[REMEMBER]))
        type(self).groups = ["delegate", "web"]  # the launcher's settings changed meanwhile
        with self.assertRaisesRegex(TaskError, "no longer holds"):
            self.manager.send("user-one", record["id"], "more", limits=LIMITS)


def _manager(*tool_names: str, read_only: bool = True) -> Mock:
    manager = Mock()
    manager.server.return_value = SimpleNamespace(name="Fake")
    manager.list_tools.return_value = [McpTool(n, "d", {"type": "object", "properties": {}}, read_only) for n in tool_names]
    return manager


def _names(toolset) -> list[str]:
    return [spec["function"]["name"] for spec in toolset.specs]


class McpBuildTests(unittest.TestCase):
    def test_disposable_chat_gets_no_mcp_tools_whatever_they_are_called_or_flagged(self) -> None:
        manager = _manager("whoami", "add_event", read_only=True)
        toolset = build_mcp_tools(manager, "u", {"fake": AgentMcpConfig()}, disposable=True, level=Level.ADMIN)
        self.assertEqual((toolset.specs, toolset.handlers, toolset.notes), ([], {}, []))
        manager.list_tools.assert_not_called()  # not even a server round trip

    def test_agent_allow_and_deny_apply_to_the_model_facing_names(self) -> None:
        manager = _manager("a", "b", "c")
        selection = {"fake": AgentMcpConfig()}
        self.assertEqual(_names(build_mcp_tools(manager, "u", selection, deny=["fake__b"], level=Level.ADMIN)), ["fake__a", "fake__c"])
        self.assertEqual(_names(build_mcp_tools(manager, "u", selection, allow=["fake__c", "query_memory"], level=Level.ADMIN)), ["fake__c"])
        self.assertEqual(_names(build_mcp_tools(manager, "u", selection, allow=[], level=Level.ADMIN)), [])
        self.assertEqual(_names(build_mcp_tools(manager, "u", selection, allow=None, deny=None, level=Level.ADMIN)), ["fake__a", "fake__b", "fake__c"])

    def test_duplicate_names_are_rejected_not_overwritten(self) -> None:
        with self.assertRaisesRegex(McpToolNameError, "already used"):
            build_mcp_tools(_manager("add.event", "add_event"), "u", {"fake": AgentMcpConfig()}, level=Level.ADMIN)

    def test_a_name_of_a_builtin_tool_is_rejected(self) -> None:
        with self.assertRaisesRegex(McpToolNameError, "built-in"):
            build_mcp_tools(_manager("x"), "u", {"fake": AgentMcpConfig()}, reserved={"fake__x"}, level=Level.ADMIN)


class McpAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name))
        patcher = patch("src.agent.agent.VectorIndex.for_agent")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _agent(self, chat_id: str, manager: Mock, **extra) -> Agent:
        config = ResolvedAgent(
            user_id="u", id="a", name="Aria", models=ModelsConfig(chat="vendor/chat"), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="", tools=True, tool_groups=["memory_read"],
            has_memory=True, associative_memory=False, mcp={"fake": AgentMcpConfig()}, **extra,
        )
        return Agent(config, Mock(spec=LLMProvider), chat_id, manager, sources=self.sources)

    def test_tools_exist_in_a_normal_chat_and_not_in_a_disposable_one(self) -> None:
        chats = self.sources.conversations()
        normal, disposable = self._agent(chats.create(), _manager("whoami")), self._agent(chats.create(disposable=True), _manager("whoami"))
        for agent in (normal, disposable):
            agent._refresh_mcp_tools()
        self.assertIn("fake__whoami", normal._tool_handlers)
        self.assertNotIn("fake__whoami", disposable._tool_handlers)
        self.assertFalse(any(name.startswith("fake__") for name in disposable._tool_handlers))

    def test_dispatch_refuses_an_mcp_tool_in_a_disposable_chat_even_if_a_handler_exists(self) -> None:
        chats = self.sources.conversations()
        agent = self._agent(chats.create(disposable=True), _manager("whoami"))
        agent._mcp_tool_names = frozenset({"fake__whoami"})
        agent._tool_handlers["fake__whoami"] = lambda **kw: "SHOULD NOT RUN"
        result = agent._run_tools([{"id": "1", "name": "fake__whoami", "arguments": {}}])[0]
        self.assertTrue(result["error"])
        self.assertNotIn("SHOULD NOT RUN", result["result"])

    def test_the_agents_deny_list_removes_an_mcp_tool(self) -> None:
        agent = self._agent(self.sources.conversations().create(), _manager("a", "b"), deny=["fake__b"])
        agent._refresh_mcp_tools()
        self.assertIn("fake__a", agent._tool_handlers)
        self.assertNotIn("fake__b", agent._tool_handlers)


if __name__ == "__main__":
    unittest.main()
