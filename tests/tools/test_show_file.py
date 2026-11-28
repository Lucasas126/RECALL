from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.agent.tool_groups import AUTOMATION_EXTRA_GROUPS, DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_TOOLS
from src.agent.toolsets.viewer import build_viewer_tools
from src.config import ceiling
from src.config.levels import Level
from src.core.sources.base import Source
from src.core.sources.registry import SourceRegistry


class FakeAgent:
    def __init__(self, root: Path, *, readable: bool = True) -> None:
        self.sources = SourceRegistry({"notes": Source("notes", root, readable=readable)})
        self.outbox: list[dict] = []
        self._turn_files: list[dict] = []
        self.config = SimpleNamespace(user_id="user-a", id="example_agent")


class ShowFileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "sub").mkdir()
        (self.root / "sub" / "recipe.md").write_text("# Pasta\n", encoding="utf-8")
        self.agent = FakeAgent(self.root)
        self.show = build_viewer_tools(self.agent)[0]

    def test_opens_a_source_file_as_a_card_without_the_bytes(self) -> None:
        result = self.show("notes:sub/recipe.md", "Dinner")
        self.assertEqual(result, "opened in the panel: recipe.md (markdown)")
        self.assertEqual(self.agent.outbox, [{
            "type": "file_card", "root": "notes", "path": "sub/recipe.md", "name": "recipe.md",
            "kind": "markdown", "agent": "example_agent", "title": "Dinner",
        }])

    def test_no_title_means_no_title_key(self) -> None:
        self.show("notes:sub/recipe.md")
        self.assertNotIn("title", self.agent.outbox[0])

    def test_refusals_leave_no_card(self) -> None:
        for path in ("notes:missing.md", "notes:sub", "notes:../x.txt", "other:x", "no-colon"):
            self.assertTrue(self.show(path).startswith("error:"), path)
        self.assertEqual(self.agent.outbox, [])
        unreadable = build_viewer_tools(FakeAgent(self.root, readable=False))[0]
        self.assertIn("not readable", unreadable("notes:sub/recipe.md"))

    def test_the_group_is_registered(self) -> None:
        self.assertEqual(TOOL_GROUP_TOOLS["viewer"], ("show_file",))
        self.assertEqual(ceiling.GROUP_MIN_LEVEL["viewer"], Level.ASSISTANT)
        self.assertIn("show_file", DISPOSABLE_BLOCKED_TOOLS)
        self.assertNotIn("viewer", AUTOMATION_EXTRA_GROUPS)


if __name__ == "__main__":
    unittest.main()
