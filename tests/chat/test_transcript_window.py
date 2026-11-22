from __future__ import annotations

import json
import os
import random
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.core.sources import conversations
from src.core.sources.conversations import ConversationsSource

ROLES = ("user", "assistant", "tool", "assistant", "user", "compaction")


def reference(turns, limit, before):
    """What the full read gave: the whole file parsed, then cut."""
    shown = [p for p, t in enumerate(turns) if t.get("role") in ("user", "assistant", "compaction")]
    everything = shown
    if before is not None:
        shown = [p for p in shown if p < before]
    chosen = shown if limit is None else shown[max(0, len(shown) - limit):]
    spoken = lambda ps: sum(1 for p in ps if turns[p].get("role") != "compaction")
    window = {"total": spoken(everything), "hidden": spoken(shown[: len(shown) - len(chosen)]), "first_index": chosen[0] if chosen else None}
    return [(p, turns[p]) for p in chosen], window


class TranscriptWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        conversations._FACTS.clear()
        conversations._INDEXES.clear()
        self.addCleanup(conversations._FACTS.clear)
        self.addCleanup(conversations._INDEXES.clear)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = ConversationsSource(self.root)
        self.chat = self.source.create()
        self.path = self.root / f"{self.chat}.jsonl"

    def fill(self, rng: random.Random, count: int) -> None:
        for number in range(count):
            role = rng.choice(ROLES)
            extra = {"note": "caf\u00e9 \u2028 \u00f1"} if rng.random() < 0.1 else {}
            self.source.append(self.chat, role, f"text {number} " + "x" * rng.randint(0, 200), ts=f"2026-11-14T10:00:{number % 60:02d}+01:00", **extra)

    def assert_same(self, limit, before, drop=False) -> None:
        turns = self.source.load(self.chat) if False else [json.loads(l) for l in self.path.read_bytes().split(b"\n") if l.strip()]
        if drop and turns and turns[-1].get("checkpoint") is True:
            turns = turns[:-1]
        self.assertEqual(self.source.window(self.chat, limit=limit, before=before, drop_checkpoint=drop), reference(turns, limit, before), (limit, before, drop))

    def test_every_limit_and_before_matches_the_full_read(self) -> None:
        rng = random.Random(7)
        for size in (0, 1, 2, 9, 40):
            self.setUp()
            self.fill(rng, size)
            for limit in (None, 1, 3, 25, 500):
                for before in (None, 0, 1, 5, size, size + 3):
                    self.assert_same(limit, before)

    def test_it_follows_appended_turns_and_a_replaced_checkpoint(self) -> None:
        rng = random.Random(3)
        self.fill(rng, 12)
        self.assert_same(5, None)
        self.fill(rng, 3)
        self.assert_same(5, None)
        self.source.checkpoint(self.chat, "partial", replace=False)
        self.assert_same(5, None)
        self.assert_same(5, None, drop=True)
        self.source.checkpoint(self.chat, "partial, a bit longer than before", replace=True)
        self.assert_same(5, None)
        self.assert_same(5, None, drop=True)
        self.source.append(self.chat, "assistant", "final", drop_checkpoint=True)
        self.assert_same(5, None, drop=True)
        self.assert_same(None, None)

    def test_a_rewound_or_replaced_transcript_is_read_again(self) -> None:
        rng = random.Random(5)
        self.fill(rng, 20)
        self.assert_same(4, None)
        first_user = next(p for p, t in enumerate(self.source.load(self.chat)) if t["role"] == "user")
        self.source.truncate_from(self.chat, first_user)
        self.assert_same(4, None)
        self.fill(rng, 4)
        self.assert_same(4, None)
        replacement = self.root / "other.tmp"
        replacement.write_text(json.dumps({"role": "user", "content": "other"}) + "\n", encoding="utf-8")
        os.replace(replacement, self.path)
        self.assert_same(4, None)
        self.assertEqual(self.source.summary(self.chat)["title"], "other")

    def test_a_window_reads_no_whole_transcript_once_indexed(self) -> None:
        self.fill(random.Random(1), 30)
        self.source.window(self.chat, limit=5)
        with patch.object(ConversationsSource, "load", side_effect=AssertionError("full read")):
            self.source.window(self.chat, limit=5)
            self.source.summary(self.chat)
            self.source.append(self.chat, "user", "one more")
            self.source.window(self.chat, limit=5)
            self.source.summary(self.chat)

    def test_a_missing_chat_and_a_damaged_line_behave_as_before(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.source.window("nope", limit=5)
        self.fill(random.Random(2), 3)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        with self.assertRaises(ValueError):
            self.source.window(self.chat, limit=5)


if __name__ == "__main__":
    unittest.main()
