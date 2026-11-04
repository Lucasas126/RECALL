from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from src.providers.claude_subscription import native
from src.providers.claude_subscription.cache import (
    QueriedTurnMismatch, cache_ready, pin_message_breakpoint, restore_queried_turn, stable_request_context,
)
from src.providers.claude_subscription.provider import ClaudeSubscriptionProvider

FAKE = str(Path(__file__).with_name("fake_claude.py"))
MARK = {"type": "ephemeral", "ttl": "1h"}
_GUARDED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def _text(text: str, **extra) -> dict:
    return {"type": "text", "text": text, **extra}


def _plain(messages: list[dict]) -> list:
    """Messages without cache marks, strings as one text block: what the cache prefix compares."""
    out = []
    for message in messages:
        content = message["content"]
        blocks = [_text(content)] if isinstance(content, str) else content
        out.append((message["role"], [{k: v for k, v in b.items() if k != "cache_control"} for b in blocks]))
    return out


def _marks(body: dict) -> list[tuple[int, int]]:
    return [(i, j) for i, m in enumerate(body["messages"]) if isinstance(m["content"], list)
            for j, b in enumerate(m["content"]) if "cache_control" in b]


class CacheTransformTests(unittest.TestCase):
    def test_opening_request_drops_prepended_context_and_marks_the_users_turn(self) -> None:
        queried = [_text("What is 2+2?")]
        payload = json.dumps({"messages": [
            {"role": "user", "content": [_text("<system-reminder>ctx</system-reminder>"), _text("What is 2+2?")]},
            {"role": "system", "content": [_text("Env.\n\nToday's date is 2026-01-02.", cache_control=MARK)]},
        ]}).encode()
        body = json.loads(pin_message_breakpoint(payload, queried))
        self.assertEqual(body["messages"][0]["content"], [_text("What is 2+2?", cache_control=MARK)])
        self.assertEqual(_marks(body), [(0, 0)])  # moved, never multiplied

    def test_follow_up_returns_the_date_line_and_drops_the_reminder(self) -> None:
        queried = [_text("And 3+3?")]
        payload = json.dumps({"messages": [
            {"role": "user", "content": [_text("What is 2+2?")]},
            {"role": "system", "content": "Env."},
            {"role": "assistant", "content": [_text("4")]},
            {"role": "user", "content": [_text("<system-reminder>ctx</system-reminder>"), _text("And 3+3?")]},
            {"role": "system", "content": [_text("Today's date is 2026-01-02.", cache_control=MARK)]},
        ]}).encode()
        payload, unrestored = cache_ready(payload, queried)
        body = json.loads(payload)
        self.assertIsNone(unrestored)
        self.assertEqual(body["messages"][1]["content"], "Env.\n\nToday's date is 2026-01-02.")
        self.assertEqual(body["messages"][-1], {"role": "user", "content": [_text("And 3+3?", cache_control=MARK)]})
        self.assertEqual(_marks(body), [(3, 0)])

    def test_unexpected_shapes_forward_unchanged(self) -> None:
        for payload in (b"not json", json.dumps({"messages": "x"}).encode(), json.dumps({"other": 1}).encode()):
            self.assertEqual(cache_ready(payload, [_text("x")])[0], payload)
        self.assertEqual(stable_request_context(b"{}", None), b"{}")

    def test_tool_results_are_restored_without_appended_context(self) -> None:
        queried = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "12:00"}]
        sent = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "12:00"},
                _text("<system-reminder>ctx</system-reminder>")]
        payload = json.dumps({"messages": [
            {"role": "user", "content": [_text("Time?")]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "t", "input": {}}]},
            {"role": "user", "content": sent},
        ]}).encode()
        body = json.loads(restore_queried_turn(payload, queried))
        self.assertEqual(body["messages"][-1]["content"], queried)
        changed = json.dumps({"messages": [
            {"role": "assistant", "content": []},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "other", "content": "x"}]},
        ]}).encode()
        with self.assertRaises(QueriedTurnMismatch):
            restore_queried_turn(changed, queried)
        self.assertEqual(cache_ready(changed, queried)[1], "the request does not uniquely match RECALL's tool results")

    def test_tool_round_sends_the_result_as_recall_queried_it_with_the_breakpoint(self) -> None:
        queried = [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "12:00"}]
        thinking = {"type": "thinking", "thinking": "need the time", "signature": "sig"}
        call = {"type": "tool_use", "id": "toolu_1", "name": "mcp__recall__get_time", "input": {}}
        payload = json.dumps({"messages": [
            {"role": "user", "content": [_text("Time?")]},
            {"role": "assistant", "content": [thinking, call]},
            {"role": "user", "content": [dict(queried[0], cache_control=MARK), _text("<system-reminder>ctx</system-reminder>")]},
        ]}).encode()
        payload, unrestored = cache_ready(payload, queried)
        body = json.loads(payload)
        self.assertIsNone(unrestored)
        # the next request replays exactly this frame, so its breakpoint is a cache hit there
        self.assertEqual(body["messages"][-1]["content"], [dict(queried[0], cache_control=MARK)])
        self.assertEqual(_marks(body), [(2, 0)])
        self.assertEqual(body["messages"][1]["content"], [thinking, call])  # signed thinking replayed untouched

    def test_session_id_is_stable_for_one_conversation(self) -> None:
        first = [{"type": "user", "message": {"role": "user", "content": [_text("Hi")]}}]
        later = first + [{"type": "assistant", "message": {"role": "assistant", "content": [_text("Hello")]}},
                         {"type": "user", "message": {"role": "user", "content": [_text("Again")]}}]
        self.assertEqual(native.session_id("system", first), native.session_id("system", later))
        self.assertNotEqual(native.session_id("system", first), native.session_id("other system", first))


class _Recorder(BaseHTTPRequestHandler):
    bodies: list = []

    def do_POST(self) -> None:
        type(self).bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        events = [
            {"type": "message_start", "message": {"id": "m", "role": "assistant", "content": [], "usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Relayed answer"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


class RelayCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.record = Path(tmp.name) / "record.json"
        env = {key: value for key, value in os.environ.items() if key not in _GUARDED}
        env["FAKE_CLAUDE_RECORD"] = str(self.record)
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        _Recorder.bodies = []
        server = HTTPServer(("127.0.0.1", 0), _Recorder)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.provider = ClaudeSubscriptionProvider(
            command=[sys.executable, FAKE, "--scenario", "native_cache"], timeout_seconds=20,
            upstream_url=f"http://127.0.0.1:{server.server_port}",
        )

    def _turn(self, messages: list[dict]) -> tuple[str, dict]:
        result = self.provider.invoke(messages, "claude_subscription:haiku", stream=False)
        recorded = json.loads(self.record.read_text(encoding="utf-8"))
        self.assertEqual(recorded["system_prompt"], "Line one\nLine two")  # byte-identical, no CRLF
        return recorded["argv"][recorded["argv"].index("--session-id") + 1], result

    def test_each_request_replays_the_previous_cached_prefix(self) -> None:
        messages = [{"role": "system", "content": "Line one\nLine two"}, {"role": "user", "content": "What is 2+2?"}]
        first_session, result = self._turn(messages)
        messages += [{"role": "assistant", "content": result["response"]}, {"role": "user", "content": "And 3+3?"}]
        second_session, _ = self._turn(messages)
        self.assertEqual(first_session, second_session)
        first, second = _Recorder.bodies
        # the first request's breakpoint sits on the user's turn, and the second request starts with it unchanged
        self.assertEqual(_marks(first), [(0, 0)])
        self.assertEqual(_plain(second["messages"])[:1], _plain(first["messages"])[:1])
        # the environment message recurs with its date, and the second breakpoint is on the newest user text
        self.assertEqual(second["messages"][1]["content"], "Environment.\n\nToday's date is 2026-01-02.")
        self.assertEqual(_plain(second["messages"])[1], _plain(first["messages"])[1])
        self.assertEqual(_marks(second), [(3, 0)])
        self.assertEqual(second["messages"][3]["content"], [_text("And 3+3?", cache_control=MARK)])
        self.assertNotIn("system-reminder", json.dumps(second))


if __name__ == "__main__":
    unittest.main()
