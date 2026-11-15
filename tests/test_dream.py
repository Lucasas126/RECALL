from __future__ import annotations

import asyncio
import json
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock

from pydantic import ValidationError

from src.agent.dream import DreamRunner
from src.config.models import AgentConfig, DreamConfig, ModelsConfig, ResolvedAgent
from src.core.review import ReviewQueue
from src.core.sources.registry import SourceRegistry
from src.service.dream import DreamDependencies, DreamManager, next_run, parse_dream_command
from src.utils.files import NonBlockingFileLock


def _config(**dream) -> ResolvedAgent:
    return ResolvedAgent(
        user_id="u", id="a", name="Aria", models=ModelsConfig(chat="vendor/chat"),
        provider_name="ollama", api_key="", embeddings_provider_name="ollama", embeddings_api_key="",
        dream=DreamConfig(**dream), tool_groups=["memory_read", "memory_edit", "self"],
    )


def _plain(content: str) -> str:
    """A turn's text without its untrusted-data wrapper."""
    return content.removeprefix('<chat_text untrusted="true">\n').removesuffix("\n</chat_text>")


class ScriptedProvider:
    """A fake model: each call returns the next scripted step; tool calls are dicts like the real providers."""

    def __init__(self, steps: list):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.gate: threading.Event | None = None

    def invoke(self, messages, model, **kwargs):
        self.calls.append({"messages": [dict(message) for message in messages], "model": model, **kwargs})
        if self.gate is not None:
            self.gate.wait(5)
        step = self.steps.pop(0) if self.steps else _finish("done")
        if isinstance(step, Exception):
            raise step
        if callable(step):
            step = step(messages)
        return step


def _call(call_id: str, name: str, **arguments) -> dict:
    return {"id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _finish(summary: str, call_id: str = "fin", **extra) -> dict:
    """A model step that ends the session with dream_finish."""
    return {"tool_calls": [_call(call_id, "dream_finish", summary=summary)], **extra}


class DreamFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=self.root)
        self.chats = self.sources.conversations()
        self.state = self.sources.dream_state()
        self.queue = ReviewQueue(self.chats, self.state)

    def chat(self, *texts: str) -> str:
        chat_id = self.chats.create()
        self.add(chat_id, *texts)
        return chat_id

    def add(self, chat_id: str, *texts: str) -> None:
        for index, text in enumerate(texts):
            self.chats.append(chat_id, "user" if index % 2 == 0 else "assistant", text)


class ReviewQueueTests(DreamFixture):
    def test_new_chats_are_pending_and_marking_uses_the_captured_count(self) -> None:
        first = self.chat("hello", "hi")
        empty = self.chats.create()
        pending = self.queue.pending()
        self.assertEqual([item["chat_id"] for item in pending], [first])
        self.assertEqual(pending[0]["new_turns"], 2)
        part = self.queue.read(first)
        self.assertEqual(part["read_until"], 2)
        self.add(first, "a message that arrives during the dream")
        self.queue.mark_reviewed(first, part["read_until"])
        pending = self.queue.pending()
        self.assertEqual(pending[0]["reviewed_until"], 2)
        self.assertEqual(pending[0]["new_turns"], 1)
        self.assertNotIn(empty, [item["chat_id"] for item in pending])

    def test_continuing_chat_shows_only_new_turns_with_context(self) -> None:
        chat_id = self.chat("one", "two", "three", "four")
        self.queue.mark_reviewed(chat_id, 4)
        self.assertEqual(self.queue.pending(), [])
        self.add(chat_id, "five", "six")
        part = self.queue.read(chat_id)
        self.assertEqual([_plain(turn["content"]) for turn in part["context"]], ["three", "four"])
        self.assertEqual([_plain(turn["content"]) for turn in part["turns"]], ["five", "six"])
        self.assertEqual(part["turns"][0]["n"], 5)

    def test_long_chats_are_read_in_parts_and_watermarks_never_go_back(self) -> None:
        chat_id = self.chat(*[f"m{n}" for n in range(10)])
        part = self.queue.read(chat_id, max_turns=4)
        self.assertEqual((part["read_until"], part["remaining_after"]), (4, 6))
        self.queue.mark_reviewed(chat_id, 4)
        self.assertEqual(self.queue.mark_reviewed(chat_id, 2), 4)
        self.assertEqual(self.queue.pending()[0]["new_turns"], 6)

    def test_a_part_stops_at_the_char_budget_and_the_next_one_continues(self) -> None:
        from src.core.review import PART_CHARS, TURN_CHARS
        chat_id = self.chat(*["w" * TURN_CHARS for _ in range(30)])
        part = self.queue.read(chat_id)
        self.assertLess(part["read_until"], 30)
        self.assertLessEqual(sum(len(turn["content"]) for turn in part["turns"]), PART_CHARS)
        following = self.queue.read(chat_id, after=part["read_until"])
        self.assertEqual(following["turns"][0]["n"], part["read_until"] + 1)

    def test_deleted_chats_are_dropped_and_order_is_oldest_first(self) -> None:
        old = self.chat("old")
        new = self.chat("new")
        state_chats = self.state.state()["chats"]
        self.assertEqual(state_chats, {})
        self.queue.mark_reviewed(old, 0)
        self.chats.delete_chat(old)
        self.assertEqual([item["chat_id"] for item in self.queue.pending()], [new])
        self.assertNotIn(old, self.state.state()["chats"])


class LockAndScheduleTests(DreamFixture):
    def test_lock_is_exclusive_across_handles(self) -> None:
        first, second = self.state.run_lock(), self.state.run_lock()
        self.assertTrue(first.acquire())
        self.assertTrue(self.state.is_running())
        self.assertFalse(second.acquire())
        first.release()
        self.assertFalse(self.state.is_running())
        self.assertTrue(second.acquire())
        second.release()
        self.assertIsInstance(first, NonBlockingFileLock)

    def test_every_hours_counts_from_the_last_start_or_the_schedule_anchor(self) -> None:
        dream = DreamConfig(enabled=True, every_hours=6)
        now = datetime(2026, 11, 3, 12, 0).astimezone()
        self.assertEqual(next_run(dream, now - timedelta(hours=1), None, now), now + timedelta(hours=5))
        self.assertEqual(next_run(dream, None, now - timedelta(hours=7), now), now - timedelta(hours=1))
        self.assertEqual(next_run(dream, None, None, now), now + timedelta(hours=6))

    def test_daily_at_is_the_first_local_slot_after_the_anchor(self) -> None:
        dream = DreamConfig(enabled=True, daily_at="04:00")
        last = datetime(2026, 11, 3, 4, 0, 30).astimezone()
        self.assertEqual(next_run(dream, last, None, last).replace(tzinfo=None), datetime(2026, 11, 4, 4, 0))
        early = datetime(2026, 11, 3, 2, 0).astimezone()
        self.assertEqual(next_run(dream, None, early, early).replace(tzinfo=None), datetime(2026, 11, 3, 4, 0))

    def test_schedule_off_and_validation(self) -> None:
        now = datetime.now().astimezone()
        self.assertIsNone(next_run(DreamConfig(enabled=True), None, None, now))
        self.assertIsNone(next_run(DreamConfig(enabled=False, every_hours=1), None, None, now))
        with self.assertRaises(ValidationError):
            DreamConfig(every_hours=6, daily_at="04:00")
        with self.assertRaises(ValidationError):
            DreamConfig(daily_at="25:00")
        with self.assertRaises(ValidationError):
            AgentConfig(id="a", name="A", dream={"every_hours": 2, "daily_at": "01:00"})

    def test_dream_command_parsing(self) -> None:
        self.assertEqual(parse_dream_command("/dream"), "status")
        self.assertEqual(parse_dream_command("  /DREAM  "), "status")
        self.assertEqual(parse_dream_command("/dream start"), "start")
        self.assertEqual(parse_dream_command("/dream@recall_bot start"), "start")
        self.assertEqual(parse_dream_command("/dream later"), "invalid")
        for text in ("/dreams", "dream", "tell me about /dream", "/dreamstart", ""):
            self.assertIsNone(parse_dream_command(text), text)


class DreamRunnerTests(DreamFixture):
    def runner(self, provider, **dream) -> DreamRunner:
        return DreamRunner(_config(**dream), provider, self.sources, session_id="20261103-040000-abcd", trigger="manual")

    def test_fake_provider_session_reviews_writes_files_and_traces_everything(self) -> None:
        chat_id = self.chat("I moved to Valencia", "Noted!")
        self.sources.agent_files().write_soul("I am Aria.")
        provider = ScriptedProvider([
            {"reasoning": "check the queue", "tool_calls": [_call("c1", "dream_pending")]},
            {"tool_calls": [_call("c2", "dream_read_chat", chat_id=chat_id)]},
            {"tool_calls": [
                _call("c3", "memory", target="user", action="add", content="Lives in Valencia"),
                _call("c4", "append", memory_path="mind_palace:journal/2026-11.md", content="- reviewed one chat"),
                _call("c5", "dream_mark_reviewed", chat_id=chat_id),
                _call("c6", "write", memory_path="conversations:x.jsonl", content="nope"),
                _call("c7", "soul_edit", action="patch", old_string="I am Aria.", new_string="I am Aria, curious."),
            ]},
            _finish("Reviewed 1 chat; learned the user's city.", usage={"prompt_tokens": 100, "completion_tokens": 20}),
        ])
        summary = self.runner(provider).run()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["chats_reviewed"], [chat_id])
        self.assertEqual((summary["pending_before"], summary["pending_after"]), (1, 0))
        self.assertEqual(summary["steps"], 4)
        self.assertEqual(summary["tool_calls"], 8)
        self.assertEqual(summary["errors"], 1)  # the refused write to conversations
        self.assertEqual(summary["summary"], "Reviewed 1 chat; learned the user's city.")
        changed = {item["path"]: item["change"] for item in summary["files_changed"]}
        self.assertEqual(changed, {
            "agent:SOUL.md": "modified", "agent:memories/USER.md": "created",
            "mind_palace:journal/2026-11.md": "created",
        })
        self.assertIn("+I am Aria, curious.", summary["diffs"]["agent:SOUL.md"])
        self.assertEqual(self.sources.agent_files().memory_entries("user"), ["Lives in Valencia"])
        self.assertEqual(self.queue.pending(), [])
        # the dream used its model, prompt and tools
        first = provider.calls[0]
        self.assertEqual(first["model"], "vendor/chat")
        self.assertEqual(first["reasoning_effort"], "medium")
        self.assertIn("## Dream session", first["messages"][0]["content"])
        self.assertTrue(first["messages"][0]["content"].startswith("I am Aria."))
        names = {spec["function"]["name"] for spec in first["tools"]}
        self.assertTrue({"dream_pending", "dream_read_chat", "dream_mark_reviewed", "dream_finish", "memory", "soul_edit", "skill_manage", "append"} <= names)
        self.assertFalse(names & {"web_search", "folder_read", "folder_write"})
        # trace and summary files
        trace = self.state.trace("20261103-040000-abcd")
        kinds = [event["type"] for event in trace]
        self.assertEqual(kinds[0], "session_started")
        self.assertEqual(kinds[-1], "session_finished")
        self.assertIn("reasoning", kinds)
        self.assertEqual(kinds.count("tool_call"), 8)
        self.assertEqual(kinds.count("tool_result"), 8)
        call = next(event for event in trace if event["type"] == "tool_call" and event["name"] == "memory")
        self.assertEqual(call["arguments"]["content"], "Lives in Valencia")
        refused = next(event for event in trace if event["type"] == "tool_result" and event["id"] == "c6")
        self.assertTrue(refused["error"])
        self.assertEqual(self.state.summary("20261103-040000-abcd")["status"], "completed")

    def test_changes_are_reported_for_an_agent_without_files_before_the_session(self) -> None:
        provider = ScriptedProvider([
            {"tool_calls": [_call("c1", "memory", target="user", action="add", content="Lives in Valencia")]},
            {"response": "Saved one fact."},
        ])
        summary = self.runner(provider).run()
        changed = {item["path"]: item["change"] for item in summary["files_changed"]}
        self.assertEqual(changed["agent:memories/USER.md"], "created")
        self.assertIn("+Lives in Valencia", summary["diffs"]["agent:memories/USER.md"])

    def test_token_budget_ends_the_session_and_steps_are_only_counted(self) -> None:
        chat_id = self.chat("hello")
        loop = {"tool_calls": [_call("c", "dream_pending")], "usage": {"prompt_tokens": 600, "completion_tokens": 600}}
        summary = self.runner(ScriptedProvider([loop] * 70 + [_finish("done")])).run()
        self.assertEqual((summary["status"], summary["steps"]), ("completed", 71))  # no step cap
        self.assertEqual(DreamConfig.model_validate({"max_steps": 60}), DreamConfig())  # the old key is ignored
        summary = self.runner(ScriptedProvider([loop] * 5), max_tokens=2000).run()
        self.assertEqual((summary["status"], summary["steps"]), ("budget_exhausted", 2))
        self.assertEqual(self.queue.pending()[0]["chat_id"], chat_id)

    def test_provider_failure_is_recorded(self) -> None:
        with self.assertLogs("recall", level="ERROR"):
            summary = self.runner(ScriptedProvider([RuntimeError("provider down")])).run()
        self.assertEqual(summary["status"], "failed")
        self.assertIn("provider down", summary["error"])
        self.assertIn("error", [event["type"] for event in self.state.trace("20261103-040000-abcd")])

    def test_marking_requires_a_read_and_custom_prompt_replaces_the_default(self) -> None:
        chat_id = self.chat("hi")
        provider = ScriptedProvider([
            {"tool_calls": [_call("c1", "dream_mark_reviewed", chat_id=chat_id)]},
            {"response": "ok"},
        ])
        self.runner(provider, prompt="Only tidy the palace.").run()
        self.assertIn("read", provider.calls[1]["messages"][-1]["content"])
        self.assertIn("Only tidy the palace.", provider.calls[0]["messages"][0]["content"])
        self.assertNotIn("## Dream session", provider.calls[0]["messages"][0]["content"])
        self.assertEqual(len(self.queue.pending()), 1)


class DreamFinishTests(DreamFixture):
    def runner(self, provider, **dream) -> DreamRunner:
        return DreamRunner(_config(**dream), provider, self.sources, session_id="20261103-040000-abcd", trigger="manual")

    def test_a_reply_without_tool_calls_does_not_end_the_session(self) -> None:
        chat_id = self.chat("I moved to Valencia", "Noted!")
        provider = ScriptedProvider([
            {"response": "I am short on time, so I will stop here."},
            {"tool_calls": [_call("c1", "dream_read_chat", chat_id=chat_id)]},
            {"tool_calls": [_call("c2", "dream_mark_reviewed", chat_id=chat_id)]},
            _finish("Reviewed 1 chat."),
        ])
        summary = self.runner(provider).run()
        self.assertEqual((summary["status"], summary["summary"]), ("completed", "Reviewed 1 chat."))
        self.assertEqual(summary["chats_reviewed"], [chat_id])
        nudge = provider.calls[1]["messages"][-1]
        self.assertEqual(nudge["role"], "user")
        self.assertIn("1 chat(s) are still pending", nudge["content"])
        self.assertIn("dream_finish", nudge["content"])
        self.assertEqual(provider.calls[1]["messages"][-2], {"role": "assistant", "content": "I am short on time, so I will stop here."})
        self.assertEqual(len(_events_of(self.state, "continue_nudge")), 1)
        self.assertEqual(len(_events_of(self.state, "finish_requested")), 1)

    def test_dream_finish_ends_after_the_rest_of_its_batch_runs(self) -> None:
        chat_id = self.chat("hi")
        provider = ScriptedProvider([
            {"tool_calls": [_call("c1", "dream_read_chat", chat_id=chat_id)]},
            {"tool_calls": [_finish("Done.")["tool_calls"][0], _call("c2", "dream_mark_reviewed", chat_id=chat_id)]},
            _finish("never reached"),
        ])
        summary = self.runner(provider).run()
        self.assertEqual(summary["summary"], "Done.")
        self.assertEqual(summary["steps"], 2)
        self.assertEqual(self.queue.pending(), [])

    def test_an_empty_summary_is_refused_and_the_session_goes_on(self) -> None:
        provider = ScriptedProvider([
            {"tool_calls": [_call("c1", "dream_finish", summary="  ")]},
            _finish("Nothing pending."),
        ])
        summary = self.runner(provider).run()
        self.assertEqual((summary["steps"], summary["errors"], summary["summary"]), (2, 1, "Nothing pending."))

    def test_a_model_that_never_calls_dream_finish_is_closed_after_a_few_idle_replies(self) -> None:
        provider = ScriptedProvider([{"response": "bye"}] * 10)
        summary = self.runner(provider).run()
        self.assertEqual((summary["status"], summary["steps"], summary["summary"]), ("completed", 3, "bye"))
        self.assertEqual(len(_events_of(self.state, "continue_nudge")), 2)
        self.assertEqual(len(_events_of(self.state, "idle_closed")), 1)

    def test_long_chats_are_read_in_parts_and_marked_to_the_end(self) -> None:
        chat_id = self.chat(*[f"turn {n}" for n in range(1, 6)])
        provider = ScriptedProvider([
            {"tool_calls": [_call("c1", "dream_read_chat", chat_id=chat_id, max_turns=2)]},
            {"tool_calls": [_call("c2", "dream_read_chat", chat_id=chat_id, max_turns=2)]},
            {"tool_calls": [_call("c3", "dream_read_chat", chat_id=chat_id, max_turns=2)]},
            {"tool_calls": [_call("c4", "dream_read_chat", chat_id=chat_id, max_turns=2)]},
            {"tool_calls": [_call("c5", "dream_mark_reviewed", chat_id=chat_id)]},
            _finish("ok"),
        ])
        self.runner(provider).run()
        parts = [json.loads(event["result"]) for event in _events_of(self.state, "tool_result") if event["name"] == "dream_read_chat"]
        self.assertEqual([(part["read_from"], part["read_until"]) for part in parts], [(1, 2), (3, 4), (5, 5), (5, 5)])
        self.assertEqual([turn["n"] for turn in parts[1]["turns"]], [3, 4])
        self.assertEqual([turn["n"] for turn in parts[1]["context"]], [1, 2])
        self.assertIn("note", parts[3])
        self.assertEqual(self.queue.pending(), [])

    def test_the_default_prompt_points_memory_at_the_palace(self) -> None:
        provider = ScriptedProvider([_finish("ok")])
        self.runner(provider).run()
        prompt = provider.calls[0]["messages"][0]["content"]
        for phrase in ("no size limit", "pointers", "INDEX.md", "dream_finish", "There is no hurry"):
            self.assertIn(phrase, prompt)


class DreamTasksTests(DreamFixture):
    def runner(self, provider) -> DreamRunner:
        return DreamRunner(_config(), provider, self.sources, session_id="20261103-040000-abcd", trigger="manual")

    def test_open_tasks_are_listed_and_finish_is_refused_once_while_one_is_open(self) -> None:
        palace = self.sources.mind_palace()
        palace.write("dream_tasks.md", "# Dream tasks\n- [x] old one -> mind_palace:old.md\n- [ ] research heat pumps\n")
        done = "# Dream tasks\n- [x] old one -> mind_palace:old.md\n- [x] research heat pumps -> mind_palace:topics/heat.md\n"
        provider = ScriptedProvider([
            _finish("Early.", call_id="f1"),
            {"tool_calls": [_call("w1", "write", memory_path="mind_palace:dream_tasks.md", content=done)]},
            _finish("Did the task.", call_id="f2"),
        ])
        summary = self.runner(provider).run()
        opening = provider.calls[0]["messages"][1]["content"]
        self.assertIn("- research heat pumps", opening)
        self.assertNotIn("old one", opening)
        refusal = provider.calls[1]["messages"][-1]["content"]
        self.assertIn("still open", refusal)
        self.assertIn("research heat pumps", refusal)
        self.assertEqual((summary["status"], summary["summary"]), ("completed", "Did the task."))

    def test_a_second_finish_ends_the_session_even_with_tasks_open(self) -> None:
        self.sources.mind_palace().write("dream_tasks.md", "- [ ] cannot do this yet\n")
        provider = ScriptedProvider([_finish("one", call_id="f1"), _finish("Left it open: needs the user.", call_id="f2")])
        summary = self.runner(provider).run()
        self.assertEqual((summary["steps"], summary["summary"]), (2, "Left it open: needs the user."))

    def test_without_tasks_the_opening_says_so_and_finish_is_immediate(self) -> None:
        provider = ScriptedProvider([_finish("Nothing to do.")])
        summary = self.runner(provider).run()
        self.assertIn("No open dream tasks", provider.calls[0]["messages"][1]["content"])
        self.assertEqual(summary["steps"], 1)


def _events_of(state, kind: str) -> list[dict]:
    return [event for event in state.trace("20261103-040000-abcd") if event["type"] == kind]


class DreamManagerTests(DreamFixture):
    def manager(self, provider, config: ResolvedAgent, **deps) -> DreamManager:
        return DreamManager(DreamDependencies(
            resolve_agent=lambda user, agent: config, provider=lambda _: provider,
            registry=lambda _: SourceRegistry.for_agent("u", "a", data_dir=self.root),
            agents=lambda: [("u", "a")], **deps,
        ))

    def test_start_runs_in_background_and_rejects_a_second_session(self) -> None:
        self.chat("hello")
        provider = ScriptedProvider([{"response": "ok"}])
        provider.gate = threading.Event()
        manager = self.manager(provider, _config())
        self.addCleanup(provider.gate.set)
        first = manager.start("u", "a")
        self.assertEqual(first["status"], "started")
        self.assertEqual(manager.start("u", "a")["status"], "already_running")
        other_process = self.manager(ScriptedProvider([]), _config())
        self.assertEqual(other_process.start("u", "a")["status"], "already_running")
        self.assertTrue(manager.status("u", "a")["running"])
        provider.gate.set()
        self.assertTrue(manager.wait("u", "a", 5))
        status = manager.status("u", "a")
        self.assertFalse(status["running"])
        self.assertEqual(status["last_session"]["id"], first["session_id"])
        self.assertEqual(manager.sessions("u", "a")[0]["status"], "completed")
        self.assertIsNotNone(self.state.state()["last_started_at"])

    def test_manual_start_with_nothing_pending_runs_anyway_but_schedule_skips(self) -> None:
        manager = self.manager(ScriptedProvider([{"response": "ok"}]), _config(enabled=True, every_hours=1))
        self.assertEqual(manager.start("u", "a")["status"], "started_nothing_pending")
        manager.wait("u", "a", 5)
        self.assertEqual(manager.start("u", "a", trigger="schedule")["status"], "nothing_pending")

    def test_schedule_runs_when_only_a_dream_task_is_open(self) -> None:
        self.sources.mind_palace().write("dream_tasks.md", "- [ ] research something\n")
        manager = self.manager(ScriptedProvider([_finish("ok")]), _config(enabled=True, every_hours=1))
        self.assertEqual(manager.start("u", "a", trigger="schedule")["status"], "started_nothing_pending")
        manager.wait("u", "a", 5)

    def test_tick_starts_due_sessions_once_and_notifies(self) -> None:
        self.chat("hello")
        clock = {"now": datetime(2026, 11, 3, 3, 0).astimezone()}
        notifier = Mock()
        manager = self.manager(
            ScriptedProvider([{"response": "ok"}]), _config(enabled=True, daily_at="04:00", notify_telegram=True),
            clock=lambda: clock["now"], notifier=notifier,
        )
        self.assertEqual(manager.tick(), [])  # first sight anchors the schedule
        self.assertIsNotNone(self.state.state()["scheduled_since"])
        clock["now"] = datetime(2026, 11, 3, 4, 1).astimezone()
        started = manager.tick()
        self.assertEqual(started[0][2]["status"], "started")
        manager.wait("u", "a", 5)
        self.assertEqual(manager.tick(), [])  # last_started_at moved the next run to tomorrow
        notifier.assert_called_once()
        user_id, text = notifier.call_args.args
        self.assertEqual(user_id, "u")
        self.assertIn("Dream session completed for Aria.", text)
        self.assertIn("Agents → Aria → Dream sessions → 2026-11-03 04:01", text)

    def test_notification_failures_are_logged_not_raised(self) -> None:
        self.chat("hello")
        manager = self.manager(
            ScriptedProvider([{"response": "ok"}]), _config(notify_telegram=True),
            notifier=Mock(side_effect=RuntimeError("telegram down")),
        )
        with self.assertLogs("recall", level="ERROR"):
            manager.start("u", "a")
            manager.wait("u", "a", 5)
        self.assertEqual(manager.sessions("u", "a")[0]["status"], "completed")


class TelegramDreamCommandTests(unittest.TestCase):
    def test_handler_passes_arguments_to_the_service(self) -> None:
        from src.telegram.bot import TelegramBotState, TelegramSession, dream_command

        service = Mock()
        service.dream_command.return_value = "Dream session started."
        state = TelegramBotState(service, {7: TelegramSession("u", "a")})
        update = Mock()
        update.effective_user.id = 7
        update.effective_message.reply_text = AsyncMock()
        context = Mock()
        context.application.bot_data = {"state": state}
        context.args = ["start"]
        asyncio.run(dream_command(update, context))
        service.dream_command.assert_called_once_with("u", "a", "/dream start", channel="telegram")
        update.effective_message.reply_text.assert_awaited_once_with("Dream session started.", entities=[])

    def test_one_word_commands_call_the_same_service_command(self) -> None:
        from src.telegram.bot import TelegramBotState, TelegramSession, dream_cancel_command, dream_start_command

        for handler, expected in ((dream_start_command, "/dream start"), (dream_cancel_command, "/dream cancel")):
            with self.subTest(handler=handler.__name__):
                service = Mock()
                service.dream_command.return_value = "Done."
                state = TelegramBotState(service, {7: TelegramSession("u", "a")})
                update = Mock()
                update.effective_user.id = 7
                update.effective_message.reply_text = AsyncMock()
                context = Mock()
                context.application.bot_data = {"state": state}
                context.args = []
                asyncio.run(handler(update, context))
                service.dream_command.assert_called_once_with("u", "a", expected, channel="telegram")

    def test_commands_are_registered_and_advertised_as_one_word(self) -> None:
        from unittest.mock import patch
        from src.telegram import bot

        service = Mock()
        service.login.return_value = {"agents": []}
        with patch.object(bot, "load_allowed_users", return_value={}):
            app = bot.build_application("123:abc", service, dream_scheduler=False)
        handled = {c for h in app.handlers[0] if isinstance(h, bot.CommandHandler) for c in h.commands}
        self.assertTrue({"dream", "dreamstart", "dreamcancel"} <= handled)
        source = open(bot.__file__, encoding="utf-8").read()
        for name in ("dream", "dreamstart", "dreamcancel"):
            self.assertIn(f'BotCommand("{name}"', source)
        self.assertNotIn('BotCommand("dream start', source)


if __name__ == "__main__":
    unittest.main()
