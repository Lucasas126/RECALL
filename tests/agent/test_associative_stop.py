from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.agent import associative
from src.agent.agent import Agent
from src.config.models import ModelsConfig, ResolvedAgent
from src.core.sources.conversations import ConversationsSource
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.ollama import OllamaProvider
from src.providers.provider import LLMProvider

SLOW = 5.0  # seconds a lookup would take if Stop did not abort it
LIMIT = 1.0  # Stop must be effective within this


def _slow_embedding_server() -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            time.sleep(SLOW)
            out = json.dumps({"embeddings": [[0.1]]}).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)
            except OSError:
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class StopDuringAssociativeSearchTests(unittest.TestCase):
    def _agent(self, mode: str, execution: str) -> Agent:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry = Mock()
        registry.conversations.return_value = ConversationsSource(Path(directory.name))
        self.provider = Mock(spec=LLMProvider)
        self.provider.context_window.return_value = 10000
        config = ResolvedAgent(
            user_id="user-a", id="example_agent", name="Test",
            models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="",
            tools=False, associative_memory=mode, associative_execution=execution,
        )
        with (
            patch("src.agent.agent.SourceRegistry.for_agent", return_value=registry),
            patch("src.agent.agent.VectorIndex.for_agent"),
        ):
            agent = Agent(config, self.provider)
        return agent

    def _stop_after(self, agent: Agent, stream: bool) -> tuple[float, str]:
        """Start a reply, stop it after 0.3 s; the seconds until the turn ends and how it ended."""
        token = CancellationToken()
        out: dict = {}

        def work() -> None:
            try:
                result = agent.invoke("What is my cat called?", stream=stream, cancellation=token)
                if stream:
                    for _ in result:
                        pass
                out["end"] = "done"
            except GenerationCancelled:
                out["end"] = "cancelled"
            out["at"] = time.monotonic()

        thread = threading.Thread(target=work)
        thread.start()
        time.sleep(0.3)
        stopped_at = time.monotonic()
        token.cancel("stopped")
        thread.join(SLOW + 2)
        return out.get("at", time.monotonic()) - stopped_at, out.get("end", "hung")

    @staticmethod
    def _job_threads() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == "associative-recall" and t.is_alive()]

    def _wait_no_job(self, limit: float) -> bool:
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            if not self._job_threads():
                return True
            time.sleep(0.02)
        return False

    def _provider_that_blocks(self) -> None:
        """A model that streams for a long time; the query proposal ignores cancellation like a blocking call."""
        def invoke(messages, model, *, cancellation=None, **kwargs):
            if messages[0]["content"] == associative._SYSTEM_PROMPT:
                time.sleep(SLOW)
                yield "content", "cats"
                return
            for _ in range(200):
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                yield "content", "word "
                time.sleep(0.05)
        self.provider.invoke.side_effect = invoke

    def test_stop_ends_the_turn_at_once_with_a_slow_search(self) -> None:
        for mode in ("question_only", "llm"):
            for execution in ("sequential", "parallel"):
                for stream in (True, False):
                    with self.subTest(mode=mode, execution=execution, stream=stream):
                        agent = self._agent(mode, execution)
                        self._provider_that_blocks()
                        agent.query_memory = lambda *a, cancellation=None, **k: time.sleep(SLOW) or []
                        with patch("src.agent.associative._query_model", lambda a: (a.provider, "m", False)):
                            took, how = self._stop_after(agent, stream)
                        self.assertEqual(how, "cancelled")
                        self.assertLess(took, LIMIT)
                        self.assertEqual(agent._assoc_outcome, "skipped")
                        self.assertEqual(agent.memory_event(), {"state": "skipped"})
                        self.assertIsNone(agent._assoc_job)
                        self._wait_no_job(SLOW + 1)  # let the sleeping stand-ins end before the next case

    def test_the_search_receives_the_jobs_token_and_stops_with_the_turn(self) -> None:
        agent = self._agent("question_only", "parallel")
        self._provider_that_blocks()
        seen: list[CancellationToken] = []
        started = threading.Event()

        def query_memory(query, top_k, *, cancellation=None, **kwargs):
            seen.append(cancellation)
            started.set()
            deadline = time.monotonic() + SLOW
            while time.monotonic() < deadline:  # an embedding request that aborts when the token is cancelled
                cancellation.raise_if_cancelled()
                time.sleep(0.01)
            return []

        agent.query_memory = query_memory
        took, how = self._stop_after(agent, True)
        self.assertTrue(started.is_set())
        self.assertIsInstance(seen[0], CancellationToken)
        self.assertEqual(how, "cancelled")
        self.assertLess(took, LIMIT)
        self.assertTrue(self._wait_no_job(LIMIT), "the search thread kept running after Stop")

    def test_stopped_search_emits_nothing_after_stop(self) -> None:
        agent = self._agent("question_only", "parallel")
        self._provider_that_blocks()
        release = threading.Event()

        def query_memory(query, top_k, *, cancellation=None, **kwargs):
            release.wait(SLOW)
            return [{"address": "notes:cat.md", "context": "", "document": "x", "body": "x", "score": 0.99}]

        agent.query_memory = query_memory
        self._stop_after(agent, True)
        release.set()  # the lookup returns a hit after Stop: it must not be used
        self.assertTrue(self._wait_no_job(LIMIT))
        self.assertEqual(agent._assoc_outcome, "skipped")
        self.assertIsNone(agent._assoc_suggestion)

    def test_query_embedding_request_is_aborted_by_the_token(self) -> None:
        server = _slow_embedding_server()
        self.addCleanup(server.shutdown)
        provider = OllamaProvider(base_url=f"http://127.0.0.1:{server.server_port}")
        token = CancellationToken()
        out: dict = {}

        def work() -> None:
            try:
                provider.invoke_embeddings(["cats"], "nova", cancellation=token)
            except GenerationCancelled:
                out["end"] = "cancelled"
            out["at"] = time.monotonic()

        thread = threading.Thread(target=work)
        thread.start()
        time.sleep(0.3)
        stopped_at = time.monotonic()
        token.cancel("stopped")
        thread.join(SLOW + 2)
        self.assertEqual(out.get("end"), "cancelled")
        self.assertLess(out["at"] - stopped_at, LIMIT)


if __name__ == "__main__":
    unittest.main()
