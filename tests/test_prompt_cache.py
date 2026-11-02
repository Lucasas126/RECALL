from __future__ import annotations

import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.agent import usage
from src.agent.agent import Agent
from src.config.models import PromptCacheConfig
from src.providers.openrouter import OpenRouterProvider, with_prompt_cache


def _breakdown(prompt_tokens: int) -> dict:
    breakdown, _ = usage.turn_breakdown(
        [{"role": "system", "content": "prompt"}, {"role": "user", "content": "hi"}],
        "ok", "", prompt_tokens, 5,
    )
    return breakdown


class CacheUsageTests(unittest.TestCase):
    def test_reads_cache_read_and_write_tokens(self) -> None:
        reported = {"prompt_tokens_details": {"cached_tokens": 4958, "cache_write_tokens": 13}}
        self.assertEqual(usage.cache_tokens(reported), (4958, 13))

    def test_missing_write_count_is_zero_when_a_breakdown_exists(self) -> None:
        self.assertEqual(usage.cache_tokens({"prompt_tokens_details": {"cached_tokens": 3200}}), (3200, 0))
        self.assertEqual(usage.cache_tokens({"prompt_tokens_details": {"cached_tokens": None}}), (0, 0))

    def test_no_breakdown_is_unknown_not_zero(self) -> None:
        self.assertIsNone(usage.cache_tokens(None))
        self.assertIsNone(usage.cache_tokens({"prompt_tokens": 10}))
        self.assertIsNone(usage.cache_tokens({"prompt_tokens_details": None}))
        self.assertIsNone(usage.cache_tokens({"prompt_tokens_details": {"audio_tokens": 0}}))

    def test_report_shows_cache_line_after_the_context_summary(self) -> None:
        report = usage.format_turn(_breakdown(5000), None, False, 200000, cache=(4000, 13))
        lines = report.split("\n")
        self.assertTrue(lines[0].startswith("context:"))
        self.assertEqual(lines[1], "cache: read=4000 (80% of input) write=13")

    def test_report_omits_cache_line_when_unknown_or_approximate(self) -> None:
        self.assertNotIn("cache:", usage.format_turn(_breakdown(5000), None, False, None))
        self.assertNotIn("cache:", usage.format_turn(_breakdown(5000), None, True, None, cache=(1, 1)))


BREAKPOINT = {"type": "ephemeral"}


def _stream():
    yield "data: " + json.dumps({"choices": [{"delta": {"content": "ok"}}]})
    yield "data: [DONE]"


class PromptCacheRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenRouterProvider(prompt_cache=PromptCacheConfig())
        self.messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "second"},
            {"role": "system", "content": "[associative memory] hint"},
        ]

    def test_marks_system_prompt_and_last_conversation_message_only(self) -> None:
        marked = with_prompt_cache(self.messages)
        self.assertEqual(marked[0]["content"], [{"type": "text", "text": "persona", "cache_control": BREAKPOINT}])
        self.assertEqual(marked[3]["content"], [{"type": "text", "text": "second", "cache_control": BREAKPOINT}])
        for index in (1, 2, 4):
            self.assertIsInstance(marked[index]["content"], str)

    def test_does_not_mutate_the_agent_history(self) -> None:
        before = copy.deepcopy(self.messages)
        with_prompt_cache(self.messages)
        self.assertEqual(self.messages, before)

    def test_skips_messages_without_text_when_placing_the_second_breakpoint(self) -> None:
        messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": "look it up"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        marked = with_prompt_cache(messages)
        self.assertEqual(marked[3]["content"][0]["cache_control"], BREAKPOINT)
        self.assertEqual(marked[3]["tool_call_id"], "call_1")
        self.assertEqual(marked[2], messages[2])

    def test_marks_the_last_text_block_of_multimodal_messages(self) -> None:
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        messages = [
            {"role": "system", "content": "persona"},
            {"role": "user", "content": [{"type": "text", "text": "look"}, image]},
        ]
        content = with_prompt_cache(messages)[1]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "look", "cache_control": BREAKPOINT})
        self.assertEqual(content[1], image)

    def test_blank_system_prompt_is_left_unmarked(self) -> None:
        marked = with_prompt_cache([{"role": "system", "content": "  "}, {"role": "user", "content": "hi"}])
        self.assertEqual(marked[0]["content"], "  ")

    def test_claude_requests_get_breakpoints_and_preferred_route(self) -> None:
        with patch("src.providers.openrouter.post_chat", return_value={
            "choices": [{"message": {"content": "ok"}}],
        }) as post:
            self.provider.invoke(self.messages, "anthropic/claude-haiku-5.5", stream=False)
        payload = post.call_args.args[1]
        self.assertEqual(payload["provider"], {"zdr": True, "order": ["Amazon Bedrock"]})
        self.assertEqual(payload["messages"][0]["content"][0]["cache_control"], BREAKPOINT)

    def test_streaming_claude_alias_gets_breakpoints_and_keeps_zdr_choice(self) -> None:
        with patch("src.providers.openrouter.stream_chat", side_effect=lambda *_a, **_k: _stream()) as stream:
            list(self.provider.invoke(self.messages, "~anthropic/claude-haiku-latest", zdr=False))
        payload = stream.call_args.args[1]
        self.assertEqual(payload["provider"], {"zdr": False, "order": ["Amazon Bedrock"]})
        self.assertEqual(payload["messages"][3]["content"][0]["cache_control"], BREAKPOINT)

    def test_other_models_are_sent_unchanged(self) -> None:
        with patch("src.providers.openrouter.post_chat", return_value={
            "choices": [{"message": {"content": "ok"}}],
        }) as post:
            self.provider.invoke(self.messages, "deepseek/deepseek-chat", stream=False)
        payload = post.call_args.args[1]
        self.assertEqual(payload["provider"], {"zdr": True})
        self.assertEqual(payload["messages"], self.messages)

    def test_disabled_cache_sends_claude_requests_unchanged(self) -> None:
        provider = OpenRouterProvider(prompt_cache=PromptCacheConfig(enabled=False))
        with patch("src.providers.openrouter.post_chat", return_value={
            "choices": [{"message": {"content": "ok"}}],
        }) as post:
            provider.invoke(self.messages, "anthropic/claude-haiku-5.5", stream=False)
        payload = post.call_args.args[1]
        self.assertEqual(payload["provider"], {"zdr": True})
        self.assertEqual(payload["messages"], self.messages)

    def test_empty_provider_list_only_adds_breakpoints(self) -> None:
        provider = OpenRouterProvider(prompt_cache=PromptCacheConfig(anthropic_providers=[]))
        with patch("src.providers.openrouter.post_chat", return_value={
            "choices": [{"message": {"content": "ok"}}],
        }) as post:
            provider.invoke(self.messages, "anthropic/claude-haiku-5.5", stream=False)
        self.assertEqual(post.call_args.args[1]["provider"], {"zdr": True})


class TrimContextTests(unittest.TestCase):
    def _agent(self, retained: int, count: int) -> SimpleNamespace:
        history = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"} for i in range(count)]
        return SimpleNamespace(
            config=SimpleNamespace(conversation_messages_retained=retained),
            messages=[{"role": "system", "content": "persona"}, *history],
            _truncated=False,
        )

    def test_cut_goes_below_the_limit_so_the_prefix_stays_stable_until_it_fills_again(self) -> None:
        agent = self._agent(retained=20, count=21)
        Agent._trim_context(agent)
        self.assertEqual(len(agent.messages) - 1, 15)  # 20 - 25%
        self.assertEqual(agent.messages[1]["content"], "m6")
        self.assertTrue(agent._truncated)
        first_kept = agent.messages[1]
        for i in range(5):  # the window refills without moving its start
            agent.messages.append({"role": "user", "content": f"new{i}"})
            Agent._trim_context(agent)
            self.assertIs(agent.messages[1], first_kept)

    def test_within_the_limit_nothing_is_cut(self) -> None:
        agent = self._agent(retained=20, count=20)
        Agent._trim_context(agent)
        self.assertEqual(len(agent.messages) - 1, 20)
        self.assertFalse(agent._truncated)

    def test_zero_slack_keeps_the_exact_window(self) -> None:
        agent = self._agent(retained=20, count=21)
        with patch("src.agent.agent.PROMPT_CACHE", PromptCacheConfig(trim_slack_percent=0)):
            Agent._trim_context(agent)
        self.assertEqual(len(agent.messages) - 1, 20)

    def test_tiny_windows_are_not_reduced_below_one_message(self) -> None:
        agent = self._agent(retained=1, count=2)
        Agent._trim_context(agent)
        self.assertEqual([m["content"] for m in agent.messages[1:]], ["m1"])

    def test_orphan_tool_result_is_still_dropped_after_a_cut(self) -> None:
        agent = self._agent(retained=4, count=5)
        agent.messages[-3] = {"role": "tool", "tool_call_id": "call_1", "content": "result"}
        Agent._trim_context(agent)
        self.assertNotEqual(agent.messages[1]["role"], "tool")


if __name__ == "__main__":
    unittest.main()
