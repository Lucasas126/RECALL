from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.sources.conversations import ConversationsSource
from src.utils.i18n import InputError


class ChatTitleTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = ConversationsSource(Path(directory.name))
        self.chat = self.source.create()
        self.source.append(self.chat, "user", "first question")

    def test_rename_replaces_the_default_and_generated_title(self) -> None:
        self.assertEqual(self.source.summary(self.chat)["title"], "first question")
        self.source.set_generated_title(self.chat, "Generated")
        self.assertEqual(self.source.rename(self.chat, "  My   chat "), "My chat")
        self.assertEqual(self.source.summary(self.chat)["title"], "My chat")

    def test_generated_title_never_overwrites_a_stored_title(self) -> None:
        self.source.rename(self.chat, "Mine")
        self.assertEqual(self.source.set_generated_title(self.chat, "Generated"), "Mine")
        self.assertEqual(self.source.summary(self.chat)["title"], "Mine")

    def test_rename_rejects_empty_and_too_long_titles(self) -> None:
        with self.assertRaises(InputError):
            self.source.rename(self.chat, "   ")
        with self.assertRaises(InputError):
            self.source.rename(self.chat, "x" * 61)
        self.assertEqual(self.source.summary(self.chat)["title"], "first question")

    def test_rename_unknown_chat_raises_not_found(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.source.rename("missing", "Title")


if __name__ == "__main__":
    unittest.main()
