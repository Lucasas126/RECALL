from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from test_index import IndexFixture
from src import knowledge
from src.agent.dream import dream_folder_options
from src.agent.tools import chat_folder_options
from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.memory import build_memory_tools
from src.core.sources.recall_docs import RECALL_DOCS, RecallDocsSource
from src.core.sources.registry import SourceRegistry

PAGE = "recall_docs:references/permissions.md"


class RecallDocsSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)

    def registry(self, flag: bool) -> SourceRegistry:
        return SourceRegistry.for_agent("u", "a", data_dir=self.data, recall_docs=flag)

    def test_registered_only_with_the_flag(self) -> None:
        self.assertIn(RECALL_DOCS, self.registry(True).names())
        self.assertNotIn(RECALL_DOCS, self.registry(False).source_names())
        for flag in (True, False):
            config = SimpleNamespace(recall_knowledge=flag, folders=[], tool_groups=[], tools=True)
            with self.subTest(flag=flag):
                self.assertEqual(chat_folder_options(config)["recall_docs"], flag)
                self.assertEqual(dream_folder_options(config)["recall_docs"], flag)

    def test_root_is_the_package_manual(self) -> None:
        self.assertEqual(self.registry(True).recall_docs().root.resolve(), knowledge.KNOWLEDGE_DIR.resolve())

    def test_read_through_registry_and_tool(self) -> None:
        sources = self.registry(True)
        expected = (knowledge.KNOWLEDGE_DIR / "references" / "permissions.md").read_text(encoding="utf-8")
        self.assertEqual(sources.read(PAGE), expected)
        host = SimpleNamespace(sources=sources, query_memory=lambda *a, **k: [], chat_id=None)
        tools = {f.tool_spec["function"]["name"]: f for f in build_memory_tools(host)}
        self.assertNotIn("error", tools["read"](memory_path=PAGE)[:40])

    def test_writes_are_refused(self) -> None:
        sources = self.registry(True)
        before = sources.read(PAGE)
        with self.assertRaises(PermissionError):
            sources.write(PAGE, "x")
        with self.assertRaises(PermissionError):
            sources.delete(PAGE)
        host = SimpleNamespace(sources=sources, note_written=lambda address: None)
        tools = {f.tool_spec["function"]["name"]: f for f in build_edit_tools(host)}
        outcomes = [
            tools["write"](memory_path=PAGE, content="x"),
            tools["delete"](memory_path=PAGE),
            tools["move"](memory_path=PAGE, new_path="mind_palace:moved.md"),
        ]
        for outcome in outcomes:
            self.assertIn("read-only", outcome)
        self.assertEqual(sources.read(PAGE), before)

    def test_index_scan_only_markdown(self) -> None:
        scan = RecallDocsSource().index_scan({"md", "txt"})
        self.assertIn("SKILL.md", scan)
        self.assertIn("references/permissions.md", scan)
        root = self.data / "docs"
        (root / "__pycache__").mkdir(parents=True)
        (root / "__pycache__" / "x.md").write_text("x", encoding="utf-8")
        (root / ".hidden.md").write_text("x", encoding="utf-8")
        (root / "notes.txt").write_text("x", encoding="utf-8")
        (root / "page.md").write_text("x", encoding="utf-8")
        self.assertEqual(list(RecallDocsSource(root).index_scan({"md", "txt"})), ["page.md"])


class RecallDocsIndexTests(IndexFixture):
    def registry(self, folders=None, recall_docs: bool = True) -> SourceRegistry:
        return SourceRegistry.for_agent("u", "a", data_dir=self.data, folders=[], recall_docs=recall_docs)

    def addresses(self) -> set[str]:
        return {meta["address"] for meta in self.all_chunks().values()}

    def test_docs_are_indexed_queryable_and_purged_when_off(self) -> None:
        self.index.sync()
        self.assertIn("recall_docs:SKILL.md", self.addresses())
        self.assertIn(PAGE, self.addresses())
        hits = self.index.query("permissions levels admin", top_k=200)
        self.assertTrue(any(hit["address"].startswith("recall_docs:") for hit in hits))
        off = self.make_index(sources=self.registry(recall_docs=False))
        off.sync()
        self.assertFalse(any(a.startswith("recall_docs:") for a in self.addresses()))
        self.assertIn("mind_palace:pets.md", self.addresses())
        self.assertFalse(any(hit.get("source") == RECALL_DOCS for hit in off.query("permissions", top_k=200)))


if __name__ == "__main__":
    unittest.main()
