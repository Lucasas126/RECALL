"""The query model's input (a quoted transcript, not real turns) and the guard on what it proposes."""
from __future__ import annotations

import unittest

from src.agent import associative


class TranscriptTests(unittest.TestCase):
    def test_history_is_one_quoted_block_with_cut_messages(self) -> None:
        long = "word " * 400
        text = associative._transcript(
            [{"role": "user", "content": "hello"}, {"role": "assistant", "content": long}], "what about my PC?",
        )
        self.assertIn("<transcript>\nUSER: hello\nASSISTANT: ", text)
        self.assertIn(" [...]", text)
        self.assertIn("<latest_user_message>\nwhat about my PC?\n</latest_user_message>", text)
        self.assertLess(len(text), 2 * associative._TRANSCRIPT_MESSAGE_CHARS)

    def test_empty_history_and_multimodal_text(self) -> None:
        self.assertIn("(no earlier messages)", associative._transcript([], "hi"))
        parts = [{"type": "text", "text": "look at this"}, {"type": "image_url", "image_url": {"url": "x"}}]
        self.assertEqual(associative._text(parts), "look at this")


class CleanQueryTests(unittest.TestCase):
    def test_keyword_queries_pass(self) -> None:
        for raw, expected in (
            ("mi PC hardware specs", "mi PC hardware specs"),
            ('"catedral de Salamanca estilo"\n', "catedral de Salamanca estilo"),
            ("RECALL", "RECALL"),
            ("git commits RECALL", "git commits RECALL"),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(associative._clean_query(raw), expected)

    def test_none_in_any_form_is_no_search(self) -> None:
        for raw in ("NONE", "none.", "Ninguna", "nada", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(associative._clean_query(raw))

    def test_answers_markdown_and_commands_are_discarded(self) -> None:
        for raw in (
            "I have recorded everything we discussed in the note `RECALL-GAPS.md`, including:",
            "## Paso 3: Adaptar memorias",
            "```bash",
            "ssh atlas -- bash -s",
            "**Status: complete.** All tasks",
            "- first point",
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(associative._clean_query(raw))


if __name__ == "__main__":
    unittest.main()
