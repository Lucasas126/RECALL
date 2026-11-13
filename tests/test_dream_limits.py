from __future__ import annotations

import os
import subprocess
import sys
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from src.agent.dream import DreamRunner, wrap_up_lead_seconds
from src.config.settings import _public_url
from src.core.sources.registry import SourceRegistry
from src.providers.cancellation import GenerationCancelled
from src.service.dream import DreamDependencies, DreamManager, dream_notification, session_link
from test_dream import DreamFixture, ScriptedProvider, _call, _config, _finish

SESSION = "20261103-040000-abcd"
REPO = Path(__file__).resolve().parents[1]


class FakeClock:
    """A monotonic clock the tests move by hand: no real waiting for minutes-long limits."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BlockingProvider:
    """A slow model call: advances the clock past the deadline (optional), then blocks until released.

    With `honour_token` it ends as soon as its cancellation token is cancelled (like the OpenRouter
    transport); without it, it ignores the token and keeps its thread until `release` is set.
    """

    def __init__(self, clock: FakeClock | None = None, advance: float = 0.0, *, honour_token: bool,
                 reply: dict | None = None):
        self.clock, self.advance, self.honour_token = clock, advance, honour_token
        self.reply = reply or {"response": "late reply"}
        self.calls: list[dict] = []
        self.release = threading.Event()
        self.entered = threading.Event()
        self.ended = threading.Event()

    def invoke(self, messages, model, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        try:
            if self.clock is not None:
                self.clock.advance(self.advance)
            self.entered.set()
            token = kwargs["cancellation"]
            while not self.release.wait(0.01):
                if self.honour_token:
                    token.raise_if_cancelled()
            return self.reply
        finally:
            self.ended.set()


def _events(state, kind: str) -> list[dict]:
    return [event for event in state.trace(SESSION) if event["type"] == kind]


class DreamTimeLimitTests(DreamFixture):
    def runner(self, provider, clock: FakeClock, **dream) -> DreamRunner:
        return DreamRunner(
            _config(**dream), provider, self.sources, session_id=SESSION, trigger="manual",
            monotonic_clock=clock, poll_seconds=0.01, abort_grace_seconds=0.2,
        )

    def test_max_minutes_default_and_bounds(self) -> None:
        from pydantic import ValidationError

        from src.config.models import DreamConfig

        self.assertEqual(DreamConfig().max_minutes, 5)
        for value in (0, 121, 2.5):
            with self.assertRaises(ValidationError):
                DreamConfig(max_minutes=value)

    def test_wrap_up_lead_is_one_minute_or_half_of_short_limits(self) -> None:
        self.assertEqual(wrap_up_lead_seconds(300), 60)
        self.assertEqual(wrap_up_lead_seconds(120), 60)
        self.assertEqual(wrap_up_lead_seconds(60), 30)

    def test_warning_is_injected_once_before_the_next_model_call_when_one_minute_remains(self) -> None:
        clock = FakeClock()

        def after(seconds: float, step: dict):
            def run(messages):
                clock.advance(seconds)
                return step
            return run

        pending = {"tool_calls": [_call("c", "dream_pending")]}
        provider = ScriptedProvider([
            after(230, pending),  # 70 s left: no warning yet
            after(20, pending),   # 50 s left: warning before the next call
            after(5, pending),
            _finish("wrapped up"),
        ])
        summary = self.runner(provider, clock, max_minutes=5).run()
        self.assertEqual(summary["status"], "completed")

        def warnings(call: dict) -> list[str]:
            return [m["content"] for m in call["messages"] if m["role"] == "user" and m["content"].startswith("[RECALL]")]

        self.assertEqual([len(warnings(call)) for call in provider.calls], [0, 0, 1, 1])
        text = warnings(provider.calls[2])[0]
        self.assertEqual(provider.calls[2]["messages"][-1]["content"], text)  # injected right before that call
        for phrase in ("About 1 minute left", "journal", "dream_mark_reviewed", "dream_finish", "closed"):
            self.assertIn(phrase, text)
        traced = _events(self.state, "wrap_up_warning")
        self.assertEqual(len(traced), 1)
        self.assertEqual((traced[0]["step"], traced[0]["seconds_left"]), (3, 50))
        self.assertIsNotNone(summary["timing"]["warning_at"])
        self.assertIsNone(summary["timing"]["stop_requested_at"])

    def test_one_minute_sessions_are_warned_halfway(self) -> None:
        clock = FakeClock()

        def step(seconds: float):
            def run(messages):
                clock.advance(seconds)
                return {"tool_calls": [_call("c", "dream_pending")]}
            return run

        provider = ScriptedProvider([step(25), step(10), {"response": "ok"}])
        self.runner(provider, clock, max_minutes=1).run()
        self.assertEqual(_events(self.state, "wrap_up_warning")[0]["step"], 3)  # 25 s left at the 3rd call
        self.assertNotIn("[RECALL]", provider.calls[1]["messages"][-1]["content"])

    def test_deadline_during_a_model_call_aborts_it_without_another_call(self) -> None:
        chat_id = self.chat("hello")
        clock = FakeClock()
        # the late reply would write a memory: it must be discarded, never applied
        provider = BlockingProvider(clock, advance=301, honour_token=False, reply={
            "tool_calls": [_call("c1", "memory", target="user", action="add", content="late fact")],
        })
        self.addCleanup(provider.release.set)
        with self.assertLogs("recall", level="WARNING") as logs:
            summary = self.runner(provider, clock, max_minutes=5).run()
        self.assertIn("still running", "\n".join(logs.output))
        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(provider.calls[0]["cancellation"]._cancelled)  # the HTTP request is told to stop
        closed = _events(self.state, "force_closed")
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["message"], "deadline reached, model call aborted")
        self.assertTrue(closed[0]["aborted_model_call"])
        self.assertFalse(closed[0]["model_thread_ended"])  # this fake ignores its token
        provider.release.set()
        self.assertTrue(provider.ended.wait(5))
        self.assertEqual(_events(self.state, "tool_call"), [])
        self.assertEqual(self.sources.agent_files().memory_entries("user"), [])
        self.assertEqual(self.queue.pending()[0]["chat_id"], chat_id)  # unmarked chats stay pending
        timing = summary["timing"]
        self.assertEqual(timing["max_minutes"], 5)
        self.assertIsNotNone(timing["stop_requested_at"])
        self.assertEqual(timing["closed_at"], summary["finished_at"])
        self.assertEqual(
            datetime.fromisoformat(timing["deadline_at"]) - datetime.fromisoformat(summary["started_at"]),
            __import__("datetime").timedelta(minutes=5),
        )
        self.assertEqual(_events(self.state, "session_finished")[0]["status"], "timed_out")

    def test_a_provider_that_honours_its_token_ends_its_thread_at_once(self) -> None:
        clock = FakeClock()
        provider = BlockingProvider(clock, advance=301, honour_token=True)
        self.addCleanup(provider.release.set)
        summary = self.runner(provider, clock).run()
        self.assertEqual(summary["status"], "timed_out")
        self.assertTrue(provider.ended.is_set())
        self.assertTrue(_events(self.state, "force_closed")[0]["model_thread_ended"])

    def test_deadline_during_a_tool_call_lets_it_finish_and_records_its_result(self) -> None:
        clock = FakeClock()
        provider = ScriptedProvider([{"tool_calls": [
            _call("c1", "memory", target="user", action="add", content="Lives in Valencia"),
            _call("c2", "memory", target="user", action="add", content="never saved"),
        ]}])
        original = DreamRunner._call
        finished = []

        def slow_tool(handlers, name, args, level):
            clock.advance(400)  # the deadline passes while the tool runs
            result = original(handlers, name, args, level)
            finished.append(name)
            return result

        with patch.object(DreamRunner, "_call", staticmethod(slow_tool)):
            summary = self.runner(provider, clock).run()
        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(len(provider.calls), 1)  # no model call after the tool
        self.assertEqual(finished, ["memory"])
        results = _events(self.state, "tool_result")
        self.assertEqual([event["id"] for event in results], ["c1"])
        self.assertFalse(results[0]["error"])
        self.assertEqual(self.sources.agent_files().memory_entries("user"), ["Lives in Valencia"])
        closed = _events(self.state, "force_closed")[0]
        self.assertEqual((closed["skipped_tool_calls"], closed["aborted_model_call"]), (1, False))
        self.assertEqual(closed["message"], "deadline reached, remaining tool calls skipped")

    def test_deadline_between_steps_closes_before_the_next_model_call(self) -> None:
        clock = FakeClock()
        original = DreamRunner._call

        def slow_tool(handlers, name, args, level):
            clock.advance(301)
            return original(handlers, name, args, level)

        with patch.object(DreamRunner, "_call", staticmethod(slow_tool)):
            summary = self.runner(ScriptedProvider([{"tool_calls": [_call("c1", "dream_pending")]}]), clock).run()
        self.assertEqual(summary["status"], "timed_out")
        self.assertEqual(summary["steps"], 1)
        self.assertEqual(len(_events(self.state, "tool_result")), 1)
        self.assertEqual(
            _events(self.state, "force_closed")[0]["message"], "deadline reached, session closed before the next model call",
        )


class DreamCancelTests(DreamFixture):
    def manager(self, provider, **deps) -> DreamManager:
        return DreamManager(DreamDependencies(
            resolve_agent=lambda user, agent: _config(notify_telegram=True), provider=lambda _: provider,
            registry=lambda _: SourceRegistry.for_agent("u", "a", data_dir=self.root), **deps,
        ))

    def test_cancel_in_this_process_aborts_the_model_call(self) -> None:
        self.chat("hello")
        provider = BlockingProvider(honour_token=True)
        self.addCleanup(provider.release.set)
        manager = self.manager(provider)
        started = manager.start("u", "a")
        self.assertTrue(provider.entered.wait(5))
        self.assertEqual(manager.cancel("u", "a"), {"status": "cancelling", "session_id": started["session_id"]})
        self.assertTrue(manager.wait("u", "a", 5))
        summary = self.state.summary(started["session_id"])
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual(len(provider.calls), 1)
        closed = [e for e in self.state.trace(started["session_id"]) if e["type"] == "force_closed"][0]
        self.assertEqual(closed["message"], "cancel requested, model call aborted")
        self.assertEqual(manager.cancel("u", "a")["status"], "not_running")
        self.assertEqual(len(self.queue.pending()), 1)

    def test_cancel_from_another_process_goes_through_the_flag_file(self) -> None:
        self.chat("hello")
        provider = BlockingProvider(honour_token=True)
        self.addCleanup(provider.release.set)
        notifier = Mock()
        runner_process = self.manager(provider, notifier=notifier)
        started = runner_process.start("u", "a")
        self.assertTrue(provider.entered.wait(5))
        other_process = self.manager(ScriptedProvider([]))  # same data folder, no session of its own
        result = other_process.cancel("u", "a")
        self.assertEqual(result, {"status": "cancelling", "session_id": started["session_id"]})
        self.assertEqual(self.state.pending_cancel(), started["session_id"])
        self.assertTrue(runner_process.wait("u", "a", 5))  # the runner polls the file while it waits on the model
        self.assertEqual(self.state.summary(started["session_id"])["status"], "cancelled")
        self.assertIsNone(self.state.pending_cancel())  # cleared when the session ends
        self.assertFalse(other_process.status("u", "a")["cancelling"])
        self.assertEqual(other_process.cancel("u", "a")["status"], "not_running")
        self.assertIn("Dream session cancelled for Aria.", notifier.call_args.args[1])

    def test_a_cancel_file_for_another_session_is_ignored(self) -> None:
        self.state.request_cancel("20261103-030000-0000")
        clock = FakeClock()
        runner = DreamRunner(
            _config(), ScriptedProvider([{"response": "ok"}]), self.sources, session_id=SESSION, trigger="manual",
            monotonic_clock=clock, poll_seconds=0.01,
        )
        self.assertEqual(runner.run()["status"], "completed")

    def test_cancel_file_is_checked_between_steps(self) -> None:
        def request_cancel(messages):
            self.state.request_cancel(SESSION)  # another process asks while this reply comes back
            return {"tool_calls": [_call("c1", "dream_pending"), _call("c2", "dream_pending")]}

        provider = ScriptedProvider([request_cancel, {"response": "never"}])
        runner = DreamRunner(
            _config(), provider, self.sources, session_id=SESSION, trigger="manual", monotonic_clock=FakeClock(),
            poll_seconds=0.01,
        )
        summary = runner.run()
        self.assertEqual(summary["status"], "cancelled")
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(_events(self.state, "force_closed")[0]["skipped_tool_calls"], 2)


class DreamNotificationLinkTests(unittest.TestCase):
    summary = {"id": "20261103-040000-ab12", "status": "completed", "started_at": "2026-11-03T04:00:00+01:00"}

    def test_without_public_url_the_message_is_text_only(self) -> None:
        self.assertIsNone(session_link(None, "aria", self.summary["id"]))
        self.assertIsNone(session_link("", "aria", self.summary["id"]))
        text = dream_notification("en", "Aria", self.summary, None)
        self.assertEqual(text.splitlines()[0], "**✅ Dream session completed for Aria.**")
        self.assertIn("Agents → Aria → Dream sessions →", text)
        self.assertNotIn("http", text)

    def test_with_public_url_the_message_adds_the_session_link(self) -> None:
        link = session_link("http://recall.example:8000/", "aria", self.summary["id"])
        self.assertEqual(link, "http://recall.example:8000/#agents/aria/dreams/20261103-040000-ab12")
        text = dream_notification("en", "Aria", self.summary, link)
        self.assertIn("Agents → Aria → Dream sessions →", text)
        self.assertEqual(text.splitlines()[-1], f"Open it: <{link}>")
        spanish = dream_notification("es", "Aria", {**self.summary, "status": "timed_out"}, link)
        self.assertIn("alcanzó su límite de tiempo", spanish)
        self.assertIn(f"Ábrela: <{link}>", spanish)

    def test_manager_passes_the_public_url_to_the_notification(self) -> None:
        notifier = Mock()
        manager = DreamManager(DreamDependencies(
            resolve_agent=Mock(), provider=Mock(), notifier=notifier, public_url="http://recall.example:8000",
        ))
        manager._notify(_config(notify_telegram=True), self.summary)
        self.assertIn("http://recall.example:8000/#agents/a/dreams/20261103-040000-ab12", notifier.call_args.args[1])

    def test_public_url_setting_and_environment_override(self) -> None:
        self.assertEqual(_public_url(None), "")
        self.assertEqual(_public_url(" http://recall.example:8000/ "), "http://recall.example:8000")
        with self.assertRaises(ValueError):
            _public_url("recall.example:8000")
        env = {**os.environ, "RECALL_PUBLIC_URL": "http://recall.example:8000/"}
        out = subprocess.run(
            [sys.executable, "-c", "from src.config.settings import WEB_PUBLIC_URL; print(WEB_PUBLIC_URL)"],
            cwd=REPO, env=env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertEqual(out, "http://recall.example:8000")


if __name__ == "__main__":
    unittest.main()
