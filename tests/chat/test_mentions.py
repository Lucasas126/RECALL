from __future__ import annotations

import unittest

from src.service.mentions import chat_excerpt, mention_prompt, parse_mention, reply_label


class ParseMentionTests(unittest.TestCase):
    def test_prefix_is_parsed_and_stripped(self) -> None:
        self.assertEqual(parse_mention("@sage what do you think?"), ("sage", "what do you think?"))
        self.assertEqual(parse_mention("  @Atlas-2\nhi"), ("atlas-2", "hi"))
        self.assertEqual(parse_mention("@nova"), ("nova", ""))

    def test_no_mention(self) -> None:
        for text in ("hello @sage", "@ sage hi", "@sage,hi", "email@sage x", "", None):
            self.assertIsNone(parse_mention(text))


class ExcerptTests(unittest.TestCase):
    def test_recent_turns_within_limit(self) -> None:
        turns = [{"role": "user", "content": f"message {i} " + "x" * 50} for i in range(200)]
        turns.append({"role": "tool", "content": "secret tool output"})
        turns.append({"role": "assistant", "content": "done", "author_agent": "sage"})
        text = chat_excerpt(turns, limit=500)
        self.assertLessEqual(len(text), 500)
        self.assertTrue(text.endswith("agent sage: done"))
        self.assertIn("message 199", text)
        self.assertNotIn("message 0 ", text)
        self.assertNotIn("secret", text)

    def test_long_single_turn_is_cut(self) -> None:
        text = chat_excerpt([{"role": "user", "content": "y" * 5000}], limit=300)
        self.assertLessEqual(len(text), 300)

    def test_prompt_marks_context_untrusted(self) -> None:
        prompt = mention_prompt("hi", "nova", "Nova", "user: earlier")
        self.assertIn("chat of agent Nova (nova)", prompt)
        self.assertIn("<untrusted_context", prompt)
        self.assertIn("not instructions", prompt)
        self.assertTrue(prompt.endswith("hi"))
        self.assertNotIn("untrusted", mention_prompt("hi", "nova", "Nova", ""))

    def test_reply_label(self) -> None:
        self.assertEqual(reply_label("sage", "Sage"), "[reply from agent sage (Sage)]")
        self.assertEqual(reply_label("sage", "sage"), "[reply from agent sage]")


if __name__ == "__main__":
    unittest.main()
