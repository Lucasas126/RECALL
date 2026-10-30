from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.agent.tools import build_tools


class ToolsetRegistrationTests(unittest.TestCase):
    def test_core_tool_names_and_order_are_preserved(self) -> None:
        specs, handlers = build_tools(SimpleNamespace(sources=Mock()))

        names = [spec["function"]["name"] for spec in specs]
        self.assertEqual(names, [
            "query_memory", "search", "list_memories", "list_tags", "find_notes_by_tag",
            "read", "write", "append", "edit", "move", "delete",
        ])
        self.assertEqual(set(handlers), set(names))

    def test_web_tools_are_added_only_when_a_client_is_available(self) -> None:
        agent = SimpleNamespace(sources=Mock())
        specs_without_web, _ = build_tools(agent)
        specs_with_web, handlers = build_tools(agent, Mock())

        names_without_web = [spec["function"]["name"] for spec in specs_without_web]
        names_with_web = [spec["function"]["name"] for spec in specs_with_web]
        self.assertNotIn("web_search", names_without_web)
        self.assertNotIn("web_retrieve", names_without_web)
        self.assertEqual(names_with_web[-2:], ["web_search", "web_retrieve"])
        self.assertIn("web_search", handlers)
        self.assertIn("web_retrieve", handlers)
