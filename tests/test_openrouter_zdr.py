from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

import httpx

from src.agent.agent import Agent
from src.agent.associative import _propose_query
from src.config.models import AssociativeMemoryConfig
from src.providers.openrouter import OpenRouterProvider


def _stream(text: str = "Answer"):
    yield "data: " + json.dumps({"choices": [{"delta": {"content": text}}]})
    yield "data: [DONE]"


class OpenRouterZdrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = OpenRouterProvider()

    def test_sync_defaults_to_zdr_and_preserves_chat_options(self) -> None:
        tools = [{"type": "function", "function": {"name": "read"}}]
        messages = [{"role": "user", "content": "Question"}]
        with patch("src.providers.openrouter.post_chat", return_value={
            "choices": [{"message": {"content": "Answer"}}],
        }) as post:
            result = self.provider.invoke(
                messages, "vendor/model", stream=False, tools=tools, reasoning=True, reasoning_effort="low",
            )
        payload = post.call_args.args[1]
        self.assertEqual(payload["provider"], {"zdr": True})
        self.assertEqual(payload["messages"], messages)
        self.assertEqual(payload["tools"], tools)
        self.assertEqual(payload["reasoning"], {"effort": "low"})
        self.assertFalse(payload["stream"])
        self.assertEqual(result["response"], "Answer")

    def test_stream_defaults_to_zdr(self) -> None:
        with patch("src.providers.openrouter.stream_chat", side_effect=lambda *_args, **_kwargs: _stream()) as stream:
            result = list(self.provider.invoke([], "vendor/model"))
        self.assertEqual(stream.call_args.args[1]["provider"], {"zdr": True})
        self.assertTrue(stream.call_args.args[1]["stream"])
        self.assertEqual(result[-1]["response"], "Answer")

    def test_non_streaming_retry_keeps_zdr(self) -> None:
        with (
            patch("src.providers.openrouter.stream_chat", side_effect=httpx.ConnectError("offline")) as stream,
            patch("src.providers.openrouter.post_chat", return_value={
                "choices": [{"message": {"content": "Answer"}}],
            }) as retry,
            self.assertLogs("recall.provider-openrouter", level="WARNING"),
        ):
            result = list(self.provider.invoke([], "vendor/model"))
        self.assertEqual(stream.call_args.args[1]["provider"], {"zdr": True})
        self.assertEqual(retry.call_args.args[1], {**stream.call_args.args[1], "stream": False})
        self.assertEqual(result[-1]["response"], "Answer")

    def test_no_zdr_route_errors_never_retry_without_zdr(self) -> None:
        request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
        response = httpx.Response(404, request=request, json={
            "error": {"message": "No endpoints found matching your data policy."},
        })
        error = httpx.HTTPStatusError("No route", request=request, response=response)
        for streaming in (True, False):
            with self.subTest(stream=streaming):
                with (
                    patch("src.providers.openrouter.stream_chat", side_effect=error) as stream,
                    patch("src.providers.openrouter.post_chat", side_effect=error) as post,
                ):
                    with self.assertRaisesRegex(RuntimeError, "data policy"):
                        result = self.provider.invoke([], "vendor/model", stream=streaming)
                        if streaming:
                            list(result)
                self.assertEqual(post.call_count, 0 if streaming else 1)
                called = stream if streaming else post
                self.assertEqual(called.call_args.args[1]["provider"], {"zdr": True})

    def test_associative_query_also_uses_zdr(self) -> None:
        self.enterContext(patch("src.agent.associative.ASSOC", AssociativeMemoryConfig(query_model=None)))
        agent = Mock(spec=Agent)
        agent.messages = [{"role": "system", "content": "Test prompt"}]
        agent.config = Mock()
        agent.config.models.chat = "vendor/model"
        agent.zdr_required = True
        agent.provider = self.provider
        with patch("src.providers.openrouter.stream_chat", side_effect=lambda *_args, **_kwargs: _stream("NONE")) as stream:
            self.assertIsNone(_propose_query(agent, "Question"))
        self.assertEqual(stream.call_args.args[1]["provider"], {"zdr": True})

    def test_explicit_opt_out_is_per_request_and_does_not_change_default(self) -> None:
        with patch("src.providers.openrouter.post_chat", return_value={
            "choices": [{"message": {"content": "Answer"}}],
        }) as post:
            self.provider.invoke([], "vendor/model", stream=False, zdr=False)
            self.provider.invoke([], "vendor/model", stream=False)
        self.assertEqual([call.args[1]["provider"]["zdr"] for call in post.call_args_list], [False, True])

    def test_stream_opt_out_and_retry_preserve_explicit_policy(self) -> None:
        with patch("src.providers.openrouter.stream_chat", side_effect=lambda *_args, **_kwargs: _stream()) as stream:
            list(self.provider.invoke([], "vendor/model", zdr=False))
        self.assertFalse(stream.call_args.args[1]["provider"]["zdr"])
        with (
            patch("src.providers.openrouter.stream_chat", side_effect=httpx.ConnectError("offline")),
            patch("src.providers.openrouter.post_chat", return_value={
                "choices": [{"message": {"content": "Answer"}}],
            }) as retry,
            self.assertLogs("recall.provider-openrouter", level="WARNING"),
        ):
            list(self.provider.invoke([], "vendor/model", zdr=False))
        self.assertFalse(retry.call_args.args[1]["provider"]["zdr"])

    def test_associative_query_honors_explicit_chat_opt_out(self) -> None:
        self.enterContext(patch("src.agent.associative.ASSOC", AssociativeMemoryConfig(query_model=None)))
        agent = Mock(spec=Agent)
        agent.messages = [{"role": "system", "content": "Test"}]
        agent.config = Mock()
        agent.config.models.chat = "vendor/model"
        agent.zdr_required = False
        agent.provider = self.provider
        with patch("src.providers.openrouter.stream_chat", side_effect=lambda *_args, **_kwargs: _stream("NONE")) as stream:
            self.assertIsNone(_propose_query(agent, "Question"))
        self.assertFalse(stream.call_args.args[1]["provider"]["zdr"])

    def test_associative_query_uses_configured_small_model(self) -> None:
        config = AssociativeMemoryConfig(query_model="vendor/small", api_key="TEST_ASSOC_KEY", zdr=False)
        self.enterContext(patch("src.agent.associative.ASSOC", config))
        self.enterContext(patch.dict("os.environ", {"TEST_ASSOC_KEY": "secret"}))
        agent = Mock(spec=Agent)
        agent.messages = [{"role": "system", "content": "Test"}]
        agent.config = Mock()
        agent.config.models.chat = "vendor/model"
        agent.zdr_required = True
        agent.provider = Mock()
        with patch("src.providers.openrouter.stream_chat", side_effect=lambda *_args, **_kwargs: _stream("cats")) as stream:
            self.assertEqual(_propose_query(agent, "Question"), "cats")
        agent.provider.invoke.assert_not_called()
        self.assertEqual(stream.call_args.args[1]["model"], "vendor/small")
        self.assertFalse(stream.call_args.args[1]["provider"]["zdr"])

    def test_associative_query_without_api_key_raises_an_actionable_error(self) -> None:
        config = AssociativeMemoryConfig(query_model="vendor/small", api_key="TEST_ASSOC_MISSING")
        self.enterContext(patch("src.agent.associative.ASSOC", config))
        self.enterContext(patch.dict("os.environ", {}, clear=False))
        agent = Mock(spec=Agent)
        agent.messages = [{"role": "system", "content": "Test"}]
        with self.assertRaisesRegex(RuntimeError, "TEST_ASSOC_MISSING"):
            _propose_query(agent, "Question")


if __name__ == "__main__":
    unittest.main()
