from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.agent import dream as dream_module
from src.core.review import ReviewQueue
from src.core.sources.conversations import ConversationsSource
from src.core.sources.dream import DreamStateSource


class ReviewOrderTests(unittest.TestCase):
    """A dream reads the most recently active chats first, so a later fix is seen before an earlier worry."""

    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.chats = ConversationsSource(root / "conversations")
        self.queue = ReviewQueue(self.chats, DreamStateSource(root / "state"))

    def chat(self, *stamps: str) -> str:
        chat_id = self.chats.create()
        for index, stamp in enumerate(stamps):
            self.chats.append(chat_id, "user" if index % 2 == 0 else "assistant", f"turn {index}", ts=stamp)
        return chat_id

    def order(self) -> list[str]:
        return [item["chat_id"] for item in self.queue.pending()]

    def test_the_most_recent_chat_comes_first(self) -> None:
        oldest = self.chat("2026-11-10T09:00:00+01:00")
        middle = self.chat("2026-11-11T09:00:00+01:00")
        newest = self.chat("2026-11-13T09:00:00+01:00")
        self.assertEqual(self.order(), [newest, middle, oldest])

    def test_a_chat_that_started_long_ago_but_went_on_is_recent(self) -> None:
        started_early = self.chat("2026-11-09T09:00:00+01:00", "2026-11-14T09:00:00+01:00")
        single_later = self.chat("2026-11-12T09:00:00+01:00")
        self.assertEqual(self.order(), [started_early, single_later])  # by its LAST turn, not by when it started

    def test_an_old_chat_with_a_new_message_counts_as_recent(self) -> None:
        old = self.chat("2026-11-09T09:00:00+01:00", "2026-11-09T09:01:00+01:00", "2026-11-14T10:00:00+01:00")
        self.queue.mark_reviewed(old, 2)
        other = self.chat("2026-11-12T09:00:00+01:00")
        item = self.queue.pending()[0]
        self.assertEqual(item["chat_id"], old)
        self.assertEqual((item["new_turns"], item["first_new_at"][:10], item["last_turn_at"][:10]), (1, "2026-11-14", "2026-11-14"))
        self.assertEqual(self.order(), [old, other])

    def test_equal_times_keep_a_fixed_order(self) -> None:
        first = self.chat("2026-11-12T09:00:00+01:00")
        second = self.chat("2026-11-12T09:00:00+01:00")
        self.assertEqual(self.order(), self.order())
        self.assertEqual(sorted(self.order()), sorted([first, second]))

    def test_turns_without_a_time_do_not_break_the_order(self) -> None:
        dated = self.chat("2026-11-12T09:00:00+01:00")
        undated = self.chats.create()
        (self.chats.root / f"{undated}.jsonl").write_text('{"role": "user", "content": "written by hand, no ts"}\n', encoding="utf-8")
        self.assertEqual(self.order(), [dated, undated])  # no time at all sorts as the oldest
        item = [item for item in self.queue.pending() if item["chat_id"] == undated][0]
        self.assertEqual((item["first_new_at"], item["last_turn_at"]), ("", ""))

    def test_the_last_time_is_the_last_turn_that_has_one(self) -> None:
        chat_id = self.chats.create()
        lines = ['{"role": "user", "content": "a", "ts": "2026-11-12T09:00:00+01:00"}', '{"role": "assistant", "content": "b"}']
        (self.chats.root / f"{chat_id}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(self.queue.pending()[0]["last_turn_at"], "2026-11-12T09:00:00+01:00")

    def test_the_prompt_tells_the_model_the_order_and_why(self) -> None:
        prompt = dream_module.DREAM_SYSTEM if hasattr(dream_module, "DREAM_SYSTEM") else open(dream_module.__file__, encoding="utf-8").read()
        self.assertIn("the most recent first", prompt)
        self.assertIn("do not save it as open if it was", prompt)


if __name__ == "__main__":
    unittest.main()
