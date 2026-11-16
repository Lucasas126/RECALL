from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.agent.toolsets.delegate import TaskError
from src.service.chat import ChatEvent
from src.telegram import bot as bot_module
from src.telegram.bot import (
    TelegramBotState, TelegramSession, _handle_task_event, _on_task_event, task_callback, task_command, tasks_command,
)


def record(task_id="t1", status="running", title="Find prices", result="", **extra):
    return {
        "id": task_id, "title": title, "status": status, "reason": None, "error": None,
        "started_at": "2026-11-05T10:00:00+00:00", "ended_at": "2026-11-05T10:00:42+00:00" if status != "running" else None,
        "parent": {"agent_id": "agent-a", "chat_id": "chat-1"}, "child": {"agent_id": "agent-a", "chat_id": "kid"},
        "result": {"text": result, "truncated": False} if result else None, **extra,
    }


class FakeStream:
    instances: list["FakeStream"] = []

    def __init__(self, bot, chat_id, use_draft):
        self.chat_id, self.finished, self.started = chat_id, None, False
        FakeStream.instances.append(self)

    def attach(self, state, key, content):
        pass

    async def start(self):
        self.started = True

    async def update(self, text, entities, force=False):
        pass

    async def finish(self, messages):
        self.finished = messages


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        FakeStream.instances = []
        self.records = {"t1": record(), "t2": record("t2", "done", "Old", result="x" * 3000)}
        self.manager = Mock()
        self.manager.store.get.side_effect = lambda user, tid: self.records.get(tid) if user == "test-user" else None
        self.manager.store.list.side_effect = lambda user: list(self.records.values()) if user == "test-user" else []
        self.service = Mock()
        self.service._tasks = self.manager
        self.service.list_agents.return_value = [{"id": "agent-a", "name": "Aria"}, {"id": "agent-b", "name": "Bo"}]
        self.service.list_chats.return_value = [{"id": "chat-2", "title": "Other chat"}]
        self.service._conversations.return_value.origin.return_value = "telegram"
        self.service.get_preferences.return_value = {"show_thinking": False, "language": "en"}
        self.session = TelegramSession("test-user", "agent-a", "chat-1")
        self.state = TelegramBotState(self.service, {123: self.session, 999: TelegramSession("other-user", "agent-a", "c9")})
        self.bot = Mock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=55))
        self.bot.edit_message_reply_markup = AsyncMock()
        self.context = SimpleNamespace(bot=self.bot, application=SimpleNamespace(bot_data={"state": self.state}),
                                       user_data={}, args=[])

    def event(self, kind, **extra):
        return {"kind": kind, "user_id": "test-user", "agent_id": "agent-a", "chat_id": "chat-1", "task_id": "t1", **extra}

    def message_update(self, user_id=123):
        message = SimpleNamespace(reply_text=AsyncMock())
        return SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_message=message,
                               effective_chat=SimpleNamespace(id=user_id, type="private")), message

    def query(self, data, user_id=123, message_id=55):
        query = SimpleNamespace(data=data, message=SimpleNamespace(message_id=message_id), answer=AsyncMock(),
                                edit_message_reply_markup=AsyncMock())
        return query, SimpleNamespace(effective_user=SimpleNamespace(id=user_id), callback_query=query,
                                      effective_message=query.message, effective_chat=SimpleNamespace(id=user_id, type="private"))

    def host(self, text="working", finished=False):
        """A real reply stream (the message the task's [Stop] lives on), registered like a turn's reply."""
        self.bot.send_message_draft = AsyncMock()
        message = SimpleNamespace(message_id=55, edit_text=AsyncMock())
        self.bot.send_message = AsyncMock(return_value=message)
        stream = bot_module._ReplyStream(self.bot, 123, use_draft=True)
        content = bot_module._ReplyContent()
        content.append("text", text)
        stream.attach(self.state, (123, "test-user", "agent-a", "chat-1"), content)
        return stream, message

    @staticmethod
    def callbacks(markup):
        return [] if markup is None else [b.callback_data for row in markup.inline_keyboard for b in row]


class StopButtonTests(Base):
    async def test_stop_shows_on_the_streaming_reply_only_while_the_task_runs(self) -> None:
        stream, message = self.host()
        await stream.update("working", [])
        self.bot.send_message_draft.assert_awaited()  # a plain draft until a button is needed
        await _handle_task_event(self.bot, self.state, self.event("task_started"))
        self.assertEqual(self.bot.send_message.await_count, 1)  # the reply became a real message carrying the button
        args, kwargs = self.bot.send_message.await_args
        self.assertEqual(self.callbacks(kwargs["reply_markup"]), ["tk:t1:stop"])
        self.assertIn("Stop Find prices", kwargs["reply_markup"].inline_keyboard[0][0].text)
        self.assertEqual(self.state.task_cards["t1"], "test-user")
        self.records["t1"] = record(status="done")
        await _handle_task_event(self.bot, self.state, self.event("task_finished", status="done"))
        self.assertIsNone(message.edit_text.await_args.kwargs["reply_markup"])  # same message, button gone
        self.assertEqual(self.bot.send_message.await_count, 1)  # no separate started/finished messages
        self.assertNotIn("t1", self.state.task_cards)
        self.assertNotIn("t1", self.state.task_hosts)

    async def test_a_finished_reply_only_gets_its_reply_markup_edited(self) -> None:
        stream, message = self.host()
        await stream.finish([("done talking", [])])
        self.assertEqual(self.bot.send_message.await_count, 1)
        await _handle_task_event(self.bot, self.state, self.event("task_started"))
        kwargs = self.bot.edit_message_reply_markup.await_args.kwargs
        self.assertEqual((kwargs["chat_id"], kwargs["message_id"]), (123, 55))
        self.assertEqual(self.callbacks(kwargs["reply_markup"]), ["tk:t1:stop"])
        await _handle_task_event(self.bot, self.state, self.event("task_finished", status="done"))
        self.assertIsNone(self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"])
        self.assertEqual(self.bot.send_message.await_count, 1)
        message.edit_text.assert_not_awaited()  # the text is never rewritten

    async def test_one_button_per_running_task_and_the_rest_stay(self) -> None:
        self.records["t2"] = record("t2", title="Other work")
        stream, message = self.host()
        await stream.finish([("reply", [])])
        await _handle_task_event(self.bot, self.state, self.event("task_started"))
        await _handle_task_event(self.bot, self.state, self.event("task_started", task_id="t2"))
        self.assertEqual(self.callbacks(self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]), ["tk:t1:stop", "tk:t2:stop"])
        await _handle_task_event(self.bot, self.state, self.event("task_finished", task_id="t1"))
        self.assertEqual(self.callbacks(self.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]), ["tk:t2:stop"])

    async def test_stopping_a_hosted_task_keeps_the_other_buttons(self) -> None:
        stream, _ = self.host()
        await stream.finish([("reply", [])])
        await _handle_task_event(self.bot, self.state, self.event("task_started"))
        self.manager.stop_task.return_value = {"id": "t1", "status": "cancelled"}
        query, update = self.query("tk:t1:stop")
        await task_callback(update, self.context)
        self.manager.stop_task.assert_called_once_with("test-user", "t1")
        query.edit_message_reply_markup.assert_not_awaited()  # the task's end event removes its own button

    async def test_without_a_reply_to_host_it_nothing_is_sent(self) -> None:
        await _handle_task_event(self.bot, self.state, self.event("task_started"))
        await _handle_task_event(self.bot, self.state, self.event("task_finished", status="done"))
        self.bot.send_message.assert_not_awaited()
        self.bot.edit_message_reply_markup.assert_not_awaited()
        self.assertNotIn("t1", self.state.task_hosts)

    async def test_a_reply_that_ended_long_ago_does_not_host_a_new_task(self) -> None:
        stream, _ = self.host()
        await stream.finish([("reply", [])])
        stream.ended_at -= bot_module._HOST_GRACE_SECONDS + 1
        await _handle_task_event(self.bot, self.state, self.event("task_started"))
        self.bot.edit_message_reply_markup.assert_not_awaited()

    async def test_a_web_chat_or_another_user_is_not_hosted(self) -> None:
        self.service._conversations.return_value.origin.return_value = "web"
        stream, _ = self.host()
        await _handle_task_event(self.bot, self.state, self.event("task_started", chat_id="web-chat"))
        await _handle_task_event(self.bot, self.state, {**self.event("task_started"), "user_id": "nobody"})
        self.assertEqual(self.state.task_hosts, {})

    async def test_events_hop_to_the_bot_loop_from_a_worker_thread(self) -> None:
        stream, _ = self.host()
        await stream.finish([("reply", [])])
        self.state.loop = asyncio.get_running_loop()
        done = asyncio.Event()
        self.bot.edit_message_reply_markup.side_effect = lambda *a, **k: done.set()
        thread = threading.Thread(target=_on_task_event, args=(self.bot, self.state, self.event("task_started")))
        thread.start()
        await asyncio.wait_for(done.wait(), 5)
        thread.join()


class ToolLineTests(unittest.TestCase):
    def test_a_tool_line_shows_at_tool_start_and_the_finished_event_completes_the_same_line(self) -> None:
        content = bot_module._ReplyContent()
        content.start_action({"id": "c1", "name": "task_start", "arguments": {"goal": "x", "wait": True}})
        text, _ = bot_module._final_messages(content.parts)[0]
        self.assertIn('>task_start(goal="x", wait=true)', text)
        content.add_action({"id": "c1", "name": "task_start", "arguments": {"goal": "x", "wait": True}, "result": "ok"})
        self.assertEqual(len(content.parts), 1)
        self.assertEqual(len(content.actions), 1)
        content.add_action({"id": "c2", "name": "memory", "arguments": {}})  # no tool_start seen: appended as before
        self.assertEqual(len(content.parts), 2)


class WakeTests(Base):
    async def test_wake_reply_is_streamed_without_an_update(self) -> None:
        self.service.follow.return_value = iter([ChatEvent("text", text="Found "), ChatEvent("text", text="3 prices")])
        with patch.object(bot_module, "_ReplyStream", FakeStream):
            await _handle_task_event(self.bot, self.state, self.event("task_wake", task_ids=["t1"]))
        self.service.follow.assert_called_once_with("test-user", "agent-a", "chat-1")
        stream = FakeStream.instances[0]
        self.assertEqual(stream.chat_id, 123)
        self.assertTrue(stream.started)
        self.assertIn("Found 3 prices", "".join(text for text, _ in stream.finished))
        self.assertIn("Aria", stream.finished[0][0])

    async def test_wake_of_a_non_current_chat_names_agent_and_chat(self) -> None:
        self.service.follow.return_value = iter([ChatEvent("text", text="hello")])
        with patch.object(bot_module, "_ReplyStream", FakeStream):
            await _handle_task_event(self.bot, self.state, self.event("task_wake", chat_id="chat-2"))
        self.assertIn("Other chat", FakeStream.instances[0].finished[0][0])

    async def test_nothing_is_streamed_when_no_reply_runs(self) -> None:
        self.service.follow.return_value = None
        with patch.object(bot_module, "_ReplyStream", FakeStream):
            await _handle_task_event(self.bot, self.state, self.event("task_wake"))
        self.assertEqual(FakeStream.instances, [])

    async def test_wake_waits_for_a_reply_the_user_is_streaming(self) -> None:
        self.state.busy[("test-user", "agent-a", "chat-1")] = 1
        self.service.follow.return_value = iter([ChatEvent("text", text="late")])

        async def release():
            await asyncio.sleep(0.2)
            self.state.busy.clear()

        with patch.object(bot_module, "_ReplyStream", FakeStream):
            await asyncio.gather(_handle_task_event(self.bot, self.state, self.event("task_wake")), release())
        self.service.follow.assert_called_once()


class CommandTests(Base):
    async def test_tasks_lists_the_chat_with_a_stop_button_per_running_task(self) -> None:
        self.records["t3"] = record("t3", title="Elsewhere")
        self.records["t3"]["parent"]["chat_id"] = "other"
        update, message = self.message_update()
        await tasks_command(update, self.context)
        text = message.reply_text.await_args.args[0]
        self.assertIn("Find prices", text)
        self.assertIn("Old", text)
        self.assertNotIn("Elsewhere", text)
        buttons = [b for row in message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual([b.callback_data for b in buttons], ["tk:t1:stop"])
        self.assertEqual(self.state.task_cards["t1"], "test-user")

    async def test_tasks_without_tasks_and_for_a_stranger(self) -> None:
        self.records.clear()
        update, message = self.message_update()
        await tasks_command(update, self.context)
        self.assertIn("No tasks", message.reply_text.await_args.args[0])
        update, message = self.message_update(user_id=5)
        await tasks_command(update, self.context)
        message.reply_text.assert_not_awaited()

    async def test_stop_callback_stops_the_task(self) -> None:
        self.manager.stop_task.return_value = {"id": "t1", "status": "cancelled"}
        self.state.task_cards["t1"] = "test-user"
        query, update = self.query("tk:t1:stop")
        await task_callback(update, self.context)
        self.manager.stop_task.assert_called_once_with("test-user", "t1")
        query.answer.assert_awaited_once()
        query.edit_message_reply_markup.assert_awaited_once()

    async def test_stop_callback_refuses_a_foreign_user(self) -> None:
        self.state.task_cards["t1"] = "test-user"
        query, update = self.query("tk:t1:stop", user_id=999)
        await task_callback(update, self.context)
        self.manager.stop_task.assert_not_called()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        query, update = self.query("tk:t1:stop", user_id=5)  # not a linked Telegram user at all
        await task_callback(update, self.context)
        self.manager.stop_task.assert_not_called()

    async def test_stop_error_is_shown(self) -> None:
        self.manager.stop_task.side_effect = TaskError("unknown task: 'zz'")
        query, update = self.query("tk:zz:stop")
        await task_callback(update, self.context)
        self.assertIn("unknown task", query.answer.await_args.args[0])

    async def test_task_steers_a_running_task(self) -> None:
        update, message = self.message_update()
        self.context.args = ["t1", "use", "euros"]
        await task_command(update, self.context)
        self.manager.steer.assert_called_once_with("test-user", "t1", "use euros")
        self.assertIn("t1", message.reply_text.await_args.args[0])

    async def test_task_steer_error_is_reported(self) -> None:
        self.manager.steer.side_effect = TaskError("task t2 is not running here")
        update, message = self.message_update()
        self.context.args = ["t2", "x"]
        await task_command(update, self.context)
        self.assertIn("not running", message.reply_text.await_args.args[0])

    async def test_task_shows_status_and_a_capped_result(self) -> None:
        update, message = self.message_update()
        self.context.args = ["t2"]
        await task_command(update, self.context)
        text = message.reply_text.await_args.args[0]
        self.assertIn("Old", text)
        self.assertIn("done", text)
        self.assertLess(len(text), 1700)
        self.assertTrue(text.endswith("…"))

    async def test_task_of_another_user_is_unknown_and_a_stranger_is_ignored(self) -> None:
        update, message = self.message_update(user_id=999)  # linked, but the task belongs to test-user
        self.context.args = ["t1"]
        await task_command(update, self.context)
        self.assertIn("Unknown task", message.reply_text.await_args.args[0])
        self.context.args = ["t1", "hijack"]
        await task_command(update, self.context)
        update, message = self.message_update(user_id=5)
        await task_command(update, self.context)
        message.reply_text.assert_not_awaited()
        self.manager.steer.assert_called_once_with("other-user", "t1", "hijack")  # scoped to the caller's own store


if __name__ == "__main__":
    unittest.main()
