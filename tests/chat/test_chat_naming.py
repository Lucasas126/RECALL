import json
import unittest
from unittest.mock import Mock, patch

import src.service.chat as chat
from src.config.models import ChatNamingConfig, UserConfig
from src.service.chat import ChatService


def _call(title) -> dict:
    return {"response": "", "tool_calls": [{"id": "1", "name": "set_chat_title", "arguments": json.dumps({"title": title})}]}


def _text(text: str) -> dict:
    return {"response": text, "tool_calls": None}


class ChatNamingToolTests(unittest.TestCase):
    def name(self, *replies: dict) -> tuple[Mock, Mock]:
        service = ChatService(agent_factory=Mock(), dream=Mock())
        conversations = Mock()
        conversations.generated_title.return_value = None
        conversations.set_generated_title.side_effect = lambda _chat, title: title
        provider = Mock()
        provider.invoke_sync.side_effect = list(replies)
        user = UserConfig(max_level="admin", id="user-a", name="User A", provider={"name": "openrouter"}, agents=["atlas"])
        naming = ChatNamingConfig(model="vendor/small", min_messages=1)
        with patch.object(service, "_conversations", return_value=conversations), \
                patch.object(service, "_load", return_value=[{"role": "user", "content": "hi"}]), \
                patch.object(service, "_user", return_value=user), patch.object(chat, "CHAT_NAMING", naming), \
                patch.object(chat, "get_secret", return_value="key"), \
                patch.object(chat, "OpenRouterProvider", return_value=provider):
            service._name_chat("user-a", "atlas", "c")
        return conversations, provider

    def test_the_title_comes_from_the_tool_call(self) -> None:
        conversations, provider = self.name(_call("Trip planning"))
        conversations.set_generated_title.assert_called_once_with("c", "Trip planning")
        kwargs = provider.invoke_sync.call_args.kwargs
        self.assertEqual([t["function"]["name"] for t in kwargs["tools"]], ["set_chat_title"])
        self.assertEqual(kwargs["tool_choice"]["function"]["name"], "set_chat_title")
        self.assertEqual(provider.invoke_sync.call_count, 1)

    def test_a_plain_text_reply_is_never_stored(self) -> None:
        text = _text("Wait, the instructions say no label")
        with self.assertLogs("recall.service", "WARNING"):
            conversations, provider = self.name(text, text)
        conversations.set_generated_title.assert_not_called()
        self.assertEqual(provider.invoke_sync.call_count, 2)

    def test_an_invalid_reply_is_retried_once_with_a_correction(self) -> None:
        conversations, provider = self.name(_text("Sure, here you go"), _call("Trip planning"))
        conversations.set_generated_title.assert_called_once_with("c", "Trip planning")
        retry = provider.invoke_sync.call_args_list[1].args[0]
        self.assertEqual(retry[-1]["role"], "user")
        self.assertIn("set_chat_title", retry[-1]["content"])

    def test_two_invalid_replies_store_nothing(self) -> None:
        with self.assertLogs("recall.service", "WARNING"):
            conversations, provider = self.name(_call("Title: Trip"), _call(""))
        conversations.set_generated_title.assert_not_called()
        self.assertEqual(provider.invoke_sync.call_count, 2)

    def test_overlong_and_multiline_titles_are_rejected(self) -> None:
        for bad in ("x" * 36, "Trip\nplanning", "Suggested title: Trip", "  ", "T" * 200):
            with self.subTest(bad=bad), self.assertLogs("recall.service", "WARNING"):
                conversations, _ = self.name(_call(bad), _call(bad))
                conversations.set_generated_title.assert_not_called()

    def test_malformed_arguments_are_rejected(self) -> None:
        broken = {"response": "", "tool_calls": [{"id": "1", "name": "set_chat_title", "arguments": "{oops"}]}
        other = {"response": "", "tool_calls": [{"id": "1", "name": "other", "arguments": '{"title": "Trip"}'}]}
        with self.assertLogs("recall.service", "WARNING"):
            conversations, _ = self.name(broken, other)
        conversations.set_generated_title.assert_not_called()


if __name__ == "__main__":
    unittest.main()
