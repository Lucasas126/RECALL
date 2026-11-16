from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from src.agent.toolsets.sessions import build_session_tools
from src.core.sources.registry import SourceRegistry


def _write_chat(root: Path, chat_id: str, turns: list[tuple[str, str]]) -> None:
    lines = [
        json.dumps({"role": role, "content": text, "ts": f"2026-11-1{n % 9}T10:00:00+01:00", "actions": [{"result": "SECRET-ACTION"}]})
        for n, (role, text) in enumerate(turns)
    ]
    (root / f"{chat_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


class SessionSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name))
        self.root = self.sources.conversations().root
        self.root.mkdir(parents=True, exist_ok=True)
        _write_chat(self.root, "20261110-100000-aaaaaa", [("user", "Plan the Release Script today"), ("assistant", "Sure, the release script needs a dry run.")])
        _write_chat(self.root, "20261112-100000-bbbbbb", [("user", "unrelated"), ("assistant", "nothing here")])
        _write_chat(self.root, "20261112-110000-cccccc", [("user", "release script in a worker chat")])
        (self.root / "20261112-110000-cccccc.disposable.json").write_text('{"created_at": "2026-11-12T11:00:00"}')
        self.agent = SimpleNamespace(sources=self.sources, chat_id="20261112-100000-bbbbbb", query_memory=Mock(return_value=[]))
        self.search = build_session_tools(self.agent)[0]

    def test_quoted_query_finds_exact_text_with_excerpt_and_skips_actions(self) -> None:
        out = self.search('"release script"')
        self.assertIn("conversations:20261110-100000-aaaaaa.jsonl:1", out)
        self.assertIn("Release Script today", out)
        self.assertNotIn("SECRET-ACTION", out)
        self.assertIn("user:", out)
        self.assertIn("assistant:", out)

    def test_disposable_chats_are_never_searched(self) -> None:
        self.assertNotIn("cccccc", self.search('"release script"'))

    def test_current_chat_is_included_unless_excluded(self) -> None:
        self.assertIn("bbbbbb", self.search('"nothing here"'))
        self.assertEqual(self.search('"nothing here"', include_current=False), "no matches.")
        self.assertIn("bbbbbb", self.search('"nothing here"', chat="current"))

    def test_chat_filter_and_no_match(self) -> None:
        self.assertEqual(self.search('"release script"', chat="20261112-100000-bbbbbb"), "no matches.")
        self.assertIn("aaaaaa", self.search('"dry run"', chat="conversations:20261110-100000-aaaaaa.jsonl"))

    def test_limit_reports_the_total(self) -> None:
        out = self.search('"e"', limit=1)
        self.assertIn("showing 1 of", out)

    def test_unquoted_query_uses_the_semantic_index_on_conversations_only(self) -> None:
        self.agent.query_memory.return_value = [
            {"address": "conversations:x.jsonl", "context": "Title (2026-11-10)", "document": "d", "body": "b" * 2000, "score": 0.7}
        ]
        out = self.search("release plan")
        self.agent.query_memory.assert_called_once_with(
            "release plan", 5, exclude_chat=None, only_source="conversations", only_chat=None,
        )
        self.assertIn("(score 0.70)", out)
        self.assertLess(len(out), 800)
        self.search("release plan", include_current=False, chat="current")
        self.assertEqual(self.search("release plan", include_current=False, chat="current"), "no matches.")

    def test_unreadable_conversations(self) -> None:
        self.sources.conversations().readable = False
        self.assertTrue(self.search('"x"').startswith("error:"))


if __name__ == "__main__":
    unittest.main()
