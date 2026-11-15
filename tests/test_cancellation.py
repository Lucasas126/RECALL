from __future__ import annotations

import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import httpx

from src.agent.agent import Agent
from src.config.models import ModelsConfig, ResolvedAgent
from src.core.sources.conversations import ConversationsSource
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.ollama import OllamaProvider
from src.providers.openrouter import OpenRouterProvider
from src.providers.provider import LLMProvider
from src.providers.transport import post_chat, stream_chat


class AgentCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conversations = ConversationsSource(Path(directory.name))
        registry = Mock()
        registry.conversations.return_value = self.conversations
        self.provider = Mock(spec=LLMProvider)
        self.provider.context_window.return_value = 10000
        config = ResolvedAgent(
            user_id="test-user", id="test-agent", name="Test",
            models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="",
            tools=False, associative_memory="off", level="admin",
        )
        with (
            patch("src.agent.agent.SourceRegistry.for_agent", return_value=registry),
            patch("src.agent.agent.VectorIndex.for_agent"),
        ):
            self.agent = Agent(config, self.provider)
        self.token = CancellationToken()

    def turns(self) -> list[dict]:
        return self.conversations.load(self.agent.chat_id)

    def test_completed_stream_and_sync_keep_existing_behavior(self) -> None:
        self.provider.invoke.return_value = (event for event in [
            ("reasoning", "Thinking"), ("content", "Answer"),
            {"usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        ])
        self.assertEqual(list(self.agent.invoke("Question")), [
            ("reasoning", "Thinking"), ("content", "Answer"),
        ])
        self.assertEqual(self.turns()[-1]["content"], "Answer")
        self.assertNotIn("interrupted", self.turns()[-1])
        self.provider.invoke.return_value = {"response": "Sync answer"}
        self.assertEqual(self.agent.invoke("Again", stream=False), "Sync answer")
        self.assertFalse(self.provider.invoke.call_args.kwargs["stream"])

    def test_partial_reply_and_reasoning_survive_cancellation_and_reload(self) -> None:
        closed = threading.Event()

        def response():
            try:
                yield "reasoning", "Thinking"
                yield "content", "Partial answer"
                self.token.raise_if_cancelled()
                yield "content", " must not appear"
            finally:
                closed.set()

        self.provider.invoke.return_value = response()
        events = self.agent.invoke("Question", cancellation=self.token)
        self.assertEqual(next(events), ("reasoning", "Thinking"))
        self.assertEqual(next(events), ("content", "Partial answer"))
        self.token.cancel()
        with self.assertRaises(GenerationCancelled):
            list(events)
        self.assertTrue(closed.is_set())
        turn = self.turns()[-1]
        self.assertEqual(turn["content"], "Partial answer")
        self.assertEqual(turn["reasoning"], "Thinking")
        self.assertTrue(turn["interrupted"])
        self.assertIn("[Response interrupted by user.]", self.agent.messages[-1]["content"])
        self.assertEqual(self.agent.last_usage, "")
        self.agent.open_chat(self.agent.chat_id)
        self.assertIn("[Response interrupted by user.]", self.agent.messages[-1]["content"])
        self.assertNotIn("reasoning", self.agent.messages[-1])

    def test_non_streaming_channel_preserves_partial_reply(self) -> None:
        def response():
            yield "content", "Partial"
            self.token.cancel()
            self.token.raise_if_cancelled()

        self.provider.invoke.return_value = response()
        with self.assertRaises(GenerationCancelled):
            self.agent.invoke("Question", stream=False, cancellation=self.token)
        self.assertTrue(self.provider.invoke.call_args.kwargs["stream"])
        self.assertEqual(self.turns()[-1]["content"], "Partial")
        self.assertTrue(self.turns()[-1]["interrupted"])

    def test_cancel_before_first_token_records_empty_interrupted_reply(self) -> None:
        self.provider.invoke.return_value = (event for event in [])
        events = self.agent.invoke("Question", cancellation=self.token)
        self.token.cancel()
        with self.assertRaises(GenerationCancelled):
            list(events)
        self.provider.invoke.assert_not_called()
        self.assertEqual(self.turns()[-1]["content"], "")
        self.assertTrue(self.turns()[-1]["interrupted"])

    def test_the_interrupted_turn_records_why_it_stopped(self) -> None:
        self.provider.invoke.return_value = (event for event in [])
        events = self.agent.invoke("Question", cancellation=self.token)
        self.token.cancel("stopped")
        self.token.cancel("replaced")  # the first cause wins
        with self.assertRaises(GenerationCancelled):
            list(events)
        self.assertTrue(self.turns()[-1]["interrupted"])
        self.assertEqual(self.turns()[-1]["interrupted_by"], "stopped")

    def test_an_interruption_without_a_cause_records_none(self) -> None:
        self.token.cancel()
        with self.assertRaises(GenerationCancelled):
            self.agent.invoke("Question", cancellation=self.token)
        self.assertTrue(self.turns()[-1]["interrupted"])
        self.assertNotIn("interrupted_by", self.turns()[-1])

    def test_pre_cancelled_invocation_preserves_accepted_input(self) -> None:
        self.token.cancel()
        self.token.cancel()
        with self.assertRaises(GenerationCancelled):
            self.agent.invoke("Question", cancellation=self.token)
        self.assertEqual([turn["role"] for turn in self.turns()], ["user", "assistant"])
        self.assertEqual(self.turns()[0]["content"], "Question")
        self.assertTrue(self.turns()[1]["interrupted"])
        self.provider.invoke.assert_not_called()

    def test_tools_finish_in_flight_but_remaining_tools_do_not_run(self) -> None:
        calls = [
            {"id": "first", "name": "write", "arguments": {}},
            {"id": "second", "name": "delete", "arguments": {}},
        ]
        self.provider.invoke.return_value = (event for event in [{"tool_calls": calls}])

        def write():
            self.token.cancel()
            return "written"

        delete = Mock()
        self.agent._tool_handlers = {"write": write, "delete": delete}
        with self.assertRaises(GenerationCancelled):
            list(self.agent.invoke("Question", cancellation=self.token))
        delete.assert_not_called()
        results = [m for m in self.agent.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["first", "second"])
        self.assertEqual(results[0]["content"], "written")
        self.assertIn("not executed", results[1]["content"])
        self.assertEqual(self.turns()[-1]["actions"], [
            {"name": "write", "arguments": {}, "result": "written", "error": False},
        ])

    def test_associative_cancellation_is_not_swallowed(self) -> None:
        self.agent.config.associative_memory = "llm"
        recall_token: list[CancellationToken] = []

        def slow_recall(agent, user_input, cancellation, history=None, chat_id=None):
            recall_token.append(cancellation)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                cancellation.raise_if_cancelled()
                time.sleep(0.01)
            return None, 0

        def response():
            self.token.cancel()
            self.token.raise_if_cancelled()
            yield

        self.provider.invoke.return_value = response()
        with patch("src.agent.agent.associative.recall", side_effect=slow_recall):
            with self.assertRaises(GenerationCancelled):
                list(self.agent.invoke("Question", cancellation=self.token))
        self.assertEqual([t["role"] for t in self.turns()], ["user", "assistant"])
        self.assertTrue(self.turns()[-1]["interrupted"])
        with self.assertRaises(GenerationCancelled):
            self.provider.invoke.call_args.kwargs["cancellation"].raise_if_cancelled()
        with self.assertRaises(GenerationCancelled):
            recall_token[0].raise_if_cancelled()

    def test_new_generation_uses_fresh_token_and_interrupted_context(self) -> None:
        events = self.agent.invoke("First", cancellation=self.token)
        self.token.cancel()
        with self.assertRaises(GenerationCancelled):
            list(events)
        self.provider.invoke.return_value = (event for event in [("content", "New answer")])
        self.assertEqual(
            self.agent.invoke("Correction", stream=False, cancellation=CancellationToken()),
            "New answer",
        )
        context = self.provider.invoke.call_args.args[0]
        self.assertTrue(context[1]["content"].endswith("] First"))
        self.assertIn("[Response interrupted by user.]", context[2]["content"])
        self.assertTrue(context[3]["content"].endswith("] Correction"))


class _StalledServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _StalledHandler)
        self.started = threading.Event()
        self.disconnected = threading.Event()


class _StalledHandler(BaseHTTPRequestHandler):
    server: _StalledServer

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        if self.path != "/headers":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            if self.path == "/ollama":
                data = {"message": {"content": "Partial answer from the provider"}}
                self.wfile.write((json.dumps(data) + "\n").encode())
            elif self.path == "/openrouter":
                data = {"choices": [{"delta": {"content": "Partial answer from the provider"}}]}
                self.wfile.write(("data: " + json.dumps(data) + "\n\n").encode())
            else:
                self.wfile.write(b'{"unfinished":')
            self.wfile.flush()
        self.server.started.set()
        self.connection.settimeout(3)
        try:
            if self.connection.recv(1) == b"":
                self.server.disconnected.set()
        except TimeoutError:
            pass

    def log_message(self, format: str, *args) -> None:
        pass


class TransportCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = _StalledServer()
        thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def assert_cancels(self, operation, token: CancellationToken, ready: threading.Event) -> None:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(operation)
            self.assertTrue(ready.wait(3), "request never reached the expected checkpoint")
            started = time.monotonic()
            token.cancel()
            with self.assertRaises(GenerationCancelled):
                future.result(timeout=2)
            self.assertLess(time.monotonic() - started, 1)
            self.assertTrue(self.server.disconnected.wait(2), "HTTP connection was not closed")

    def test_cancels_before_headers(self) -> None:
        token = CancellationToken()
        self.assert_cancels(
            lambda: post_chat(self.url + "/headers", {}, cancellation=token),
            token, self.server.started,
        )

    def test_cancels_during_non_streaming_body(self) -> None:
        token = CancellationToken()
        self.assert_cancels(
            lambda: post_chat(self.url + "/body", {}, cancellation=token),
            token, self.server.started,
        )

    def test_ollama_cancels_blocked_stream_after_partial_token(self) -> None:
        token = CancellationToken()
        provider = OllamaProvider(base_url=self.url)
        received = threading.Event()
        emitted = []

        def consume():
            for chunk in provider.invoke([], "test", cancellation=token):
                emitted.append(chunk)
                received.set()

        original_stream = stream_chat
        with patch("src.providers.ollama.stream_chat", side_effect=lambda url, payload, **kw: (
            original_stream(self.url + "/ollama", payload, **kw)
        )):
            self.assert_cancels(consume, token, received)
        self.assertEqual(emitted, [("content", "Partial answer from the provider")])

    def test_openrouter_cancels_without_fallback(self) -> None:
        token = CancellationToken()
        received = threading.Event()
        emitted = []

        def consume():
            for chunk in OpenRouterProvider(api_key="unused-test-value").invoke([], "test", cancellation=token):
                emitted.append(chunk)
                received.set()

        with (
            patch("src.providers.openrouter.stream_chat", side_effect=lambda url, payload, **kw: (
                stream_chat(self.url + "/openrouter", payload, **kw)
            )),
            patch("src.providers.openrouter.post_chat") as fallback,
        ):
            self.assert_cancels(consume, token, received)
            fallback.assert_not_called()
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], "content")
        self.assertTrue(emitted[0][1].startswith("Partial answer"))

    def test_pre_cancelled_request_does_not_connect(self) -> None:
        token = CancellationToken()
        token.cancel()
        with self.assertRaises(GenerationCancelled):
            post_chat(self.url + "/headers", {}, cancellation=token)
        self.assertFalse(self.server.started.is_set())


class ProviderParsingTests(unittest.TestCase):
    def test_openrouter_fallback_emits_text_and_reasoning(self) -> None:
        def disconnected(*args, **kwargs):
            raise httpx.ConnectError("unavailable stream")
            yield

        with (
            patch("src.providers.openrouter.stream_chat", side_effect=disconnected),
            patch("src.providers.openrouter.post_chat", return_value={
                "choices": [{"message": {"content": "Answer", "reasoning": "Thinking"}}],
            }),
            self.assertLogs("recall.provider-openrouter", level="WARNING"),
        ):
            chunks = list(OpenRouterProvider().invoke([], "test"))
        self.assertEqual(chunks[:2], [("reasoning", "Thinking"), ("content", "Answer")])
        self.assertEqual(chunks[-1]["response"], "Answer")

    def test_openrouter_does_not_retry_after_partial_output(self) -> None:
        def disconnected(*args, **kwargs):
            yield "data: " + json.dumps({"choices": [{"delta": {"content": "Partial answer"}}]})
            raise httpx.ReadError("connection lost")

        with (
            patch("src.providers.openrouter.stream_chat", side_effect=disconnected),
            patch("src.providers.openrouter.post_chat") as fallback,
            self.assertLogs("recall.provider-openrouter", level="ERROR"),
        ):
            with self.assertRaises(httpx.ReadError):
                list(OpenRouterProvider().invoke([], "test"))
            fallback.assert_not_called()

    def test_openrouter_does_not_retry_if_cancelled_during_connection_failure(self) -> None:
        token = CancellationToken()

        def disconnected(*args, **kwargs):
            token.cancel()
            raise httpx.ConnectError("connection lost")
            yield

        with (
            patch("src.providers.openrouter.stream_chat", side_effect=disconnected),
            patch("src.providers.openrouter.post_chat") as fallback,
        ):
            with self.assertRaises(GenerationCancelled):
                list(OpenRouterProvider().invoke([], "test", cancellation=token))
            fallback.assert_not_called()

    def test_openrouter_keeps_reasoning_text_tools_and_usage(self) -> None:
        lines = [
            ": keep-alive",
            "data: " + json.dumps({"choices": [{"delta": {
                "reasoning": "Thinking", "content": "Answer",
                "tool_calls": [{"index": 0, "id": "call", "function": {
                    "name": "read", "arguments": '{"path":"test"}',
                }}],
            }}]}),
            "data: " + json.dumps({"choices": [], "usage": {"prompt_tokens": 12}}),
            "data: [DONE]",
        ]
        with patch("src.providers.openrouter.stream_chat", side_effect=lambda *a, **kw: (
            line for line in lines
        )):
            chunks = list(OpenRouterProvider().invoke([], "test"))
        self.assertEqual(chunks[:2], [("reasoning", "Thinking"), ("content", "Answer")])
        self.assertEqual(chunks[-1]["tool_calls"][0]["name"], "read")
        self.assertEqual(chunks[-1]["usage"], {"prompt_tokens": 12})

    def test_provider_http_error_is_not_an_interruption(self) -> None:
        request = httpx.Request("POST", "http://localhost/test")
        response = httpx.Response(400, request=request, json={"error": {"message": "bad model"}})
        error = httpx.HTTPStatusError("bad request", request=request, response=response)
        with patch("src.providers.openrouter.post_chat", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "bad model"):
                OpenRouterProvider().invoke([], "test", stream=False)


if __name__ == "__main__":
    unittest.main()
