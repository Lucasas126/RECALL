from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import MessageEntity

from src.service.chat import ChatEvent
from src.telegram.bot import (
    TelegramBotState,
    TelegramSession,
    _ReplyContent,
    _final_messages,
    _format_actions,
    _generation_message,
    _send_generation,
    _utf16_len,
)


class TelegramActionFormattingTests(unittest.TestCase):
    def test_literal_actions_are_italic_with_and_without_thinking(self) -> None:
        action = {"name": "query_memory", "arguments": {"query": "*hello* \U0001f600", "top_k": 5}}
        actions = _format_actions([action])
        self.assertEqual(actions, '>query_memory(query="*hello* \U0001f600", top_k=5)')
        for thinking in ("", "Thinking\n\n"):
            with self.subTest(thinking=thinking):
                content = _ReplyContent()
                content.append("reasoning", thinking)
                content.add_action(action)
                content.append("text", "**Answer**")
                text, entities = _generation_message(content.parts)
                self.assertIn(actions, text)
                italic = next(entity for entity in entities if entity.type == MessageEntity.ITALIC)
                prefix = text[:text.index(actions)]
                self.assertEqual(italic.offset, _utf16_len(prefix))
                self.assertEqual(italic.length, _utf16_len(actions))
                quotes = [
                    entity for entity in entities
                    if entity.type == MessageEntity.EXPANDABLE_BLOCKQUOTE
                ]
                self.assertEqual(len(quotes), 1)  # the action is in the collapsible block even with no thinking before it
                self.assertLessEqual(quotes[0].offset, italic.offset)
                self.assertGreaterEqual(
                    quotes[0].offset + quotes[0].length, italic.offset + italic.length,
                )

    def test_multiple_calls_and_empty_arguments(self) -> None:
        self.assertEqual(_format_actions([
            {"name": "list_memories", "arguments": {}},
            {"name": "query_memory", "arguments": {"sources": ["mind_palace"]}},
        ]), '>list_memories()\n>query_memory(sources=["mind_palace"])')

    def test_long_actions_survive_final_message_splitting(self) -> None:
        actions = _format_actions([
            {"name": "query", "arguments": {"text": "a \U0001f600 " * 2250}},
        ])
        parts = [("reasoning", "Thinking"), ("reasoning_tool", actions), ("text", "**Answer**")]
        messages = _final_messages(parts)
        self.assertEqual(
            "".join(text for text, _ in messages), _generation_message(parts)[0],
        )
        self.assertTrue(all(_utf16_len(text) <= 4090 for text, _ in messages))
        for text, entities in messages:
            for entity in entities:
                self.assertLessEqual(entity.offset + entity.length, _utf16_len(text))
        self.assertTrue(messages[-1][0].endswith("Answer"))

    def test_latest_chunk_keeps_latest_action_when_thinking_is_long(self) -> None:
        text, entities = _final_messages([
            ("reasoning", "x" * 5000), ("reasoning_tool", ">query()"),
        ])[-1]
        self.assertIn(">query()", text)
        self.assertTrue(any(entity.type == MessageEntity.ITALIC for entity in entities))

    def test_alternating_reasoning_text_and_tools_remain_in_order(self) -> None:
        content = _ReplyContent()
        content.append("reasoning", "Think one")
        content.add_action({"name": "first", "arguments": {}})
        content.append("reasoning", "Think two")
        content.append("text", "Explanation")
        content.add_action({"name": "second", "arguments": {}})
        content.append("reasoning", "Think three")
        content.add_action({"name": "third", "arguments": {}})
        content.append("text", "Final")
        text, entities = _generation_message(content.parts)
        expected = [
            "Think one", ">first()", "Think two", "Explanation", ">second()",
            "Think three", ">third()", "Final",
        ]
        self.assertEqual(sorted(expected, key=text.index), expected)
        quotes = [e for e in entities if e.type == MessageEntity.EXPANDABLE_BLOCKQUOTE]
        self.assertEqual(len(quotes), 2)
        for action, inside in ((">first()", True), (">second()", True), (">third()", True)):
            offset = _utf16_len(text[:text.index(action)])
            self.assertEqual(
                any(q.offset <= offset < q.offset + q.length for q in quotes), inside,
            )

    def test_an_action_before_any_thinking_or_after_text_is_inside_the_collapsible_block(self) -> None:
        content = _ReplyContent()
        content.add_action({"name": "first", "arguments": {}})
        content.append("text", "Answer")
        content.add_action({"name": "second", "arguments": {}})
        text, entities = _generation_message(content.parts)
        quotes = [e for e in entities if e.type == MessageEntity.EXPANDABLE_BLOCKQUOTE]
        self.assertEqual(len(quotes), 2)
        for action in (">first()", ">second()"):
            offset = _utf16_len(text[:text.index(action)])
            self.assertTrue(any(q.offset <= offset < q.offset + q.length for q in quotes), action)
        offset = _utf16_len(text[:text.index("Answer")])
        self.assertFalse(any(q.offset <= offset < q.offset + q.length for q in quotes))

    def test_markdown_tokens_are_coalesced_before_rendering(self) -> None:
        content = _ReplyContent()
        for token in ("**An", "swer", "**"):
            content.append("text", token)
        text, entities = _generation_message(content.parts)
        self.assertEqual(text, "Answer")
        self.assertEqual([e.type for e in entities], [MessageEntity.BOLD])

    def test_interruption_preserves_order_and_repeated_identical_calls(self) -> None:
        content = _ReplyContent()
        action = {"name": "query", "arguments": {}}
        content.append("reasoning", "Before")
        content.add_action(action)
        content.append("reasoning", "After")
        content.reconcile(ChatEvent("interrupted", "", data={
            "reasoning": "BeforeAfter", "actions": [action, action],
        }))
        text, entities = _generation_message(content.parts)
        self.assertEqual(text.count(">query()"), 2)
        self.assertLess(text.index("Before"), text.index(">query()"))
        self.assertLess(text.index(">query()"), text.index("After"))
        self.assertLess(text.index("After"), text.rindex(">query()"))
        self.assertEqual(
            len([e for e in entities if e.type == MessageEntity.EXPANDABLE_BLOCKQUOTE]), 1,
        )


class TelegramActionStreamingTests(unittest.IsolatedAsyncioTestCase):
    agent_name = "Aria"

    async def send_events(self, events: list[ChatEvent]) -> AsyncMock:
        service = Mock()
        service.send.return_value = iter(events)
        service.get_preferences.return_value = {"show_thinking": True, "language": "en"}
        service.list_agents.return_value = [{"id": "test-agent", "name": self.agent_name}]
        state = TelegramBotState(service)
        state.sessions[123] = TelegramSession("test-user", "test-agent", "test-chat")
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=456, type="private"),
        )
        context = SimpleNamespace(
            bot=Mock(), application=SimpleNamespace(bot_data={"state": state}),
        )
        stream = AsyncMock()
        with patch("src.telegram.bot._ReplyStream", return_value=stream):
            await _send_generation(update, context, "Hello", [])
        return stream

    async def test_actions_are_streamed_and_persisted_with_or_without_thinking(self) -> None:
        action = {"name": "query", "arguments": {"query": "hello"}}
        for thinking in ("", "Thinking\n"):
            with self.subTest(thinking=thinking):
                events = [ChatEvent("reasoning", thinking)] if thinking else []
                events += [ChatEvent("tool", data=action), ChatEvent("text", "Answer")]
                stream = await self.send_events(events)
                previews = [call.args[0] for call in stream.update.await_args_list]
                self.assertTrue(any('>query(query="hello")' in text for text in previews))
                messages = stream.finish.await_args.args[0]
                self.assertIn('>query(query="hello")', messages[0][0])
                self.assertIn("Answer", messages[0][0])

    async def test_interruption_reconciles_actions_without_duplicates(self) -> None:
        first = {"name": "first", "arguments": {}}
        second = {"name": "second", "arguments": {}}
        stream = await self.send_events([
            ChatEvent("reasoning", "Retained thinking\n"),
            ChatEvent("tool", data=first),
            ChatEvent("interrupted", "Partial", data={
                "reasoning": "Retained thinking\n",
                "actions": [first, second],
            }),
        ])
        text = stream.finish.await_args.args[0][0][0]
        self.assertEqual(text.count(">first()"), 1)
        self.assertEqual(text.count(">second()"), 1)
        self.assertIn("Retained thinking", text)
        self.assertIn("Partial", text)

    async def test_stream_and_final_preserve_interleaved_event_order(self) -> None:
        stream = await self.send_events([
            ChatEvent("reasoning", "Before"),
            ChatEvent("tool", data={"name": "first", "arguments": {}}),
            ChatEvent("reasoning", "After"),
            ChatEvent("text", "Explanation"),
            ChatEvent("tool", data={"name": "second", "arguments": {}}),
            ChatEvent("text", "Final"),
        ])
        final_text = stream.finish.await_args.args[0][0][0]
        expected = ["Before", ">first()", "After", "Explanation", ">second()", "Final"]
        self.assertEqual(sorted(expected, key=final_text.index), expected)
        self.assertEqual(stream.update.await_args_list[-1].args[0], final_text.replace("Aria: ", ""))

    async def test_reply_without_actions_is_unchanged(self) -> None:
        stream = await self.send_events([ChatEvent("text", "**Answer**")])
        text, entities = stream.finish.await_args.args[0][0]
        self.assertEqual(text, "Aria: Answer")
        self.assertEqual(
            [(entity.type, entity.offset, entity.length) for entity in entities],
            [(MessageEntity.BOLD, 0, 5), (MessageEntity.BOLD, 6, 6)],
        )
        for call in stream.update.await_args_list:  # drafts never carry the agent name
            self.assertNotIn("Aria", call.args[0])
        self.assertNotIn("New chat started", text)

    async def test_agent_name_goes_before_the_answer_not_inside_the_thinking(self) -> None:
        stream = await self.send_events([ChatEvent("reasoning", "Pondering"), ChatEvent("text", "Answer")])
        text, entities = stream.finish.await_args.args[0][0]
        self.assertLess(text.index("Pondering"), text.index("Aria"))
        self.assertLess(text.index("Aria"), text.index("Answer"))
        quote = next(e for e in entities if e.type == MessageEntity.EXPANDABLE_BLOCKQUOTE)
        self.assertLessEqual(quote.offset + quote.length, text.index("Aria"))
        bold = next(e for e in entities if e.type == MessageEntity.BOLD)
        self.assertEqual((bold.offset, bold.length), (text.index("Aria"), 5))
        self.assertIn("Aria: Answer", text)

    async def test_name_with_markdown_characters_and_emoji_keeps_exact_entities(self) -> None:
        self.agent_name = "my_*agent*_ 😀"
        stream = await self.send_events([ChatEvent("text", "Hi")])
        text, entities = stream.finish.await_args.args[0][0]
        self.assertTrue(text.startswith("my_*agent*_ 😀: Hi"))
        bold = entities[0]
        self.assertEqual((bold.type, bold.offset, bold.length), (MessageEntity.BOLD, 0, _utf16_len(self.agent_name) + 1))

    async def test_long_reply_splits_under_the_limit_with_the_name_only_on_the_first_message(self) -> None:
        stream = await self.send_events([ChatEvent("text", "a" * 9000)])
        messages = stream.finish.await_args.args[0]
        self.assertGreater(len(messages), 2)
        self.assertTrue(all(_utf16_len(text) <= 4096 for text, _ in messages))
        self.assertTrue(messages[0][0].startswith("Aria: aaa"))
        self.assertTrue(all("Aria" not in text for text, _ in messages[1:]))
        self.assertEqual(messages[0][1][0].offset, 0)
        self.assertEqual("".join(text for text, _ in messages), "Aria: " + "a" * 9000)
        for text, entities in messages:
            for entity in entities:
                self.assertLessEqual(entity.offset + entity.length, _utf16_len(text))

    def test_name_is_inline_unless_the_answer_starts_with_a_block(self) -> None:
        inline = ["Hola, qué tal", "\n\nHola", "**Bold** start", "*emph* start", "-5 degrees", "2024 was a year", "  two spaces"]
        for answer in inline:
            with self.subTest(answer=answer):
                text, entities = _final_messages([("text", answer)], agent_name="Aria")[0]
                self.assertTrue(text.startswith("Aria: "), text)
                self.assertEqual((entities[0].type, entities[0].offset, entities[0].length), (MessageEntity.BOLD, 0, 5))
        blocks = ["- item", "* item", "1. one", "```py\nx = 1\n```", "# Title", "> quote", "| a | b |\n|---|---|\n| 1 | 2 |", "    code", "---"]
        for answer in blocks:
            with self.subTest(answer=answer):
                text, entities = _final_messages([("text", answer)], agent_name="Aria")[0]
                self.assertTrue(text.startswith("Aria:\n"), text)
                self.assertNotIn("\n\n", text.split("Aria:", 1)[1][:2])
                self.assertEqual((entities[0].type, entities[0].offset, entities[0].length), (MessageEntity.BOLD, 0, 5))


if __name__ == "__main__":
    unittest.main()
