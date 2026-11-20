from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.sources import conversations
from src.core.sources.conversations import ConversationsSource


class SummaryCacheTests(unittest.TestCase):
    """`summary` is what the chat list is made of; it keeps what it read from a transcript until the file changes."""

    def setUp(self) -> None:
        conversations._FACTS.clear()
        self.addCleanup(conversations._FACTS.clear)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = ConversationsSource(self.root)
        self.chat = self.source.create()
        self.source.append(self.chat, "user", "first question", ts="2026-11-14T10:00:00+01:00")
        self.source.append(self.chat, "assistant", "answer", ts="2026-11-14T10:00:05+01:00")

    def loads(self):
        return patch.object(ConversationsSource, "load", autospec=True, side_effect=ConversationsSource.load)

    def test_a_second_summary_does_not_read_the_transcript_and_is_identical(self) -> None:
        first = self.source.summary(self.chat)
        with self.loads() as load:
            second = self.source.summary(self.chat)
            fresh = ConversationsSource(self.root).summary(self.chat)  # sources are built per request
        self.assertEqual(load.call_count, 0)
        self.assertEqual((first, fresh), (second, second))
        self.assertEqual((second["title"], second["turns"], second["updated"]), ("first question", 2, "2026-11-14T10:00:05+01:00"))

    def test_the_cache_gives_what_no_cache_gives(self) -> None:
        cached = self.source.summary(self.chat)
        conversations._FACTS.clear()
        self.assertEqual(cached, self.source.summary(self.chat))

    def test_an_appended_turn_shows_at_once(self) -> None:
        self.source.summary(self.chat)
        self.source.append(self.chat, "user", "next", ts="2026-11-14T11:00:00+01:00")
        summary = self.source.summary(self.chat)
        self.assertEqual((summary["turns"], summary["updated"]), (3, "2026-11-14T11:00:00+01:00"))

    def test_a_rewritten_transcript_shows_even_when_size_and_mtime_are_kept(self) -> None:
        path = self.root / f"{self.chat}.jsonl"
        before = path.stat()
        self.source.summary(self.chat)
        text = path.read_text(encoding="utf-8").replace("first question", "other question")  # same length
        self.assertEqual(len(text.encode()), before.st_size)
        replacement = self.root / "replacement.tmp"
        replacement.write_text(text, encoding="utf-8")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, path)  # a new inode with the old size and mtime, as an atomic rewrite can leave it
        self.assertEqual(self.source.summary(self.chat)["title"], "other question")

    def test_a_deleted_chat_is_not_served_from_the_cache(self) -> None:
        self.source.summary(self.chat)
        self.source.delete_chat(self.chat)
        with self.assertRaises(FileNotFoundError):
            self.source.summary(self.chat)

    def test_a_chat_with_an_attachment_first_gets_the_attachment_title(self) -> None:
        other = self.source.create()
        self.source.append(other, "user", [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}], ts="2026-11-14T10:00:00+01:00")
        self.assertEqual(self.source.summary(other)["title"], "[attachment]")
        self.assertEqual(self.source.summary(other)["title"], "[attachment]")

    def test_an_empty_chat_has_no_turns_and_uses_its_mtime(self) -> None:
        empty = self.source.create()
        summary = self.source.summary(empty)
        self.assertEqual((summary["turns"], summary["title"]), (0, ""))
        self.assertTrue(summary["updated"])

    def test_the_cache_is_bounded(self) -> None:
        with patch.object(conversations, "_FACTS_MAX", 2):
            for _ in range(3):
                chat = self.source.create()
                self.source.append(chat, "user", "x", ts="2026-11-14T10:00:00+01:00")
                self.source.summary(chat)
        self.assertLessEqual(len(conversations._FACTS), 2)


class PruneEmptyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = ConversationsSource(self.root)

    def test_blank_chats_are_removed_and_real_ones_kept(self) -> None:
        blank, whitespace, real = self.source.create(), self.source.create(), self.source.create()
        (self.root / f"{whitespace}.jsonl").write_text(" \n\n  \n", encoding="utf-8")
        self.source.append(real, "user", "hello")
        self.assertEqual(sorted(self.source.prune_empty()), sorted([blank, whitespace]))
        self.assertEqual(self.source.chats(), [real])

    def test_a_chat_that_is_blank_for_a_long_time_and_then_not_is_kept(self) -> None:
        chat = self.source.create()
        (self.root / f"{chat}.jsonl").write_text(" " * 70000 + '{"role": "user", "content": "late"}\n', encoding="utf-8")
        self.assertEqual(self.source.prune_empty(), [])
        self.assertEqual(self.source.chats(), [chat])

    def test_the_kept_chat_and_task_chats_are_not_touched(self) -> None:
        keep = self.source.create()
        self.assertEqual(self.source.prune_empty(keep), [])
        self.assertEqual(self.source.chats(), [keep])

    def test_a_young_blank_chat_is_spared_during_the_grace_period(self) -> None:
        young, old = self.source.create(), self.source.create()
        past = time.time() - 120
        os.utime(self.root / f"{old}.jsonl", (past, past))
        self.assertEqual(self.source.prune_empty(grace=30), [old])
        self.assertEqual(self.source.chats(), [young])
        self.assertEqual(self.source.prune_empty(), [young])  # no grace: removed as before

    def test_a_chat_deleted_while_listing_is_skipped(self) -> None:
        chat = self.source.create()
        real = os.stat

        def vanish(path, *args, **kwargs):
            if str(path).endswith(f"{chat}.jsonl"):
                raise FileNotFoundError(path)
            return real(path, *args, **kwargs)

        with patch("src.core.sources.conversations._is_blank", side_effect=FileNotFoundError):
            self.assertEqual(self.source.prune_empty(), [])


if __name__ == "__main__":
    unittest.main()
