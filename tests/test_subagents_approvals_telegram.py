"""A subagent task's approval is sent to the parent's Telegram chat, attributed to the task, and answered there."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from test_tasks_telegram import Base, record

from src.telegram.bot import _approval_text, _handle_task_event, approval_callback, task_attribution, tasks_command

TASK = {"id": "t1", "title": "Find prices", "kind": "ephemeral", "worker": "ephemeral"}
DATA = {"id": "0123456789ab", "tool": "terminal", "command": "ls /srv", "cwd": "~/workspace", "reason": "approvals are set to manual",
        "risk": "manual", "backend": "pc", "timeout_seconds": 300, "expires_at": 1.0, "agent_id": "agent-a", "chat_id": "chat-1",
        "task": TASK}


class TaskApprovalTelegramTests(Base):
    async def test_the_message_is_attributed_to_the_task(self) -> None:
        await _handle_task_event(self.bot, self.state, self.event("task_approval", data=DATA))
        args, kwargs = self.bot.send_message.await_args
        self.assertEqual(args[0], 123)  # the parent's Telegram chat
        self.assertIn("Task Find prices (ephemeral) wants to run:", args[1])
        self.assertIn("ls /srv", args[1])
        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual([b.callback_data.rsplit(":", 1)[1] for b in buttons], ["y", "n"])
        card = self.state.approvals[DATA["id"]]
        # registered under the PARENT chat, so resolving goes through the parent's approvals
        self.assertEqual((card.user_id, card.agent_id, card.chat_id), ("test-user", "agent-a", "chat-1"))

    async def test_the_attribution_names_a_worker_agent_and_is_absent_for_a_normal_approval(self) -> None:
        self.assertIn("(Scout)", task_attribution("en", {**TASK, "kind": "agent", "worker": "Scout"}))
        self.assertEqual(task_attribution("en", None), "")
        plain = {k: v for k, v in DATA.items() if k != "task"}
        self.assertNotIn("wants to run", _approval_text("en", plain))
        self.assertIn("La tarea Find prices", _approval_text("es", DATA))  # Markdown-escaped parentheses follow
        self.assertIn("quiere ejecutar:", _approval_text("es", DATA))

    async def test_tapping_approve_resolves_in_the_parent_chat(self) -> None:
        await _handle_task_event(self.bot, self.state, self.event("task_approval", data=DATA))
        self.service.resolve_approval.return_value = {"status": "approved"}
        self.bot.edit_message_text = AsyncMock()
        query, update = self.query(f"ap:{DATA['id']}:y", message_id=55)
        await approval_callback(update, self.context)
        self.service.resolve_approval.assert_called_once_with("test-user", "agent-a", "chat-1", DATA["id"], "approve")
        self.assertNotIn(DATA["id"], self.state.approvals)

    async def test_another_user_cannot_answer(self) -> None:
        await _handle_task_event(self.bot, self.state, self.event("task_approval", data=DATA))
        query, update = self.query(f"ap:{DATA['id']}:y", user_id=999, message_id=55)
        await approval_callback(update, self.context)
        self.service.resolve_approval.assert_not_called()
        self.assertIn(DATA["id"], self.state.approvals)

    async def test_the_resolution_closes_the_card(self) -> None:
        self.bot.edit_message_text = AsyncMock()
        await _handle_task_event(self.bot, self.state, self.event("task_approval", data=DATA))
        resolved = {"id": DATA["id"], "decision": "denied", "reason": "timed out"}
        await _handle_task_event(self.bot, self.state, self.event("task_approval_resolved", data=resolved))
        self.assertNotIn(DATA["id"], self.state.approvals)
        text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn("timed out", text)

    async def test_it_is_not_sent_to_users_without_that_chat(self) -> None:
        await _handle_task_event(self.bot, self.state, self.event("task_approval", user_id="nobody", data=DATA))
        self.bot.send_message.assert_not_awaited()

    async def test_tasks_shows_awaiting_approval(self) -> None:
        self.manager.awaiting_approval.side_effect = lambda user, task_id: 1 if task_id == "t1" else 0
        update, message = self.message_update()
        await tasks_command(update, self.context)
        text = message.reply_text.await_args.args[0]
        line = next(line for line in text.splitlines() if "t1" in line)
        self.assertIn("awaiting approval", line)
        self.assertNotIn("awaiting approval", next(line for line in text.splitlines() if "t2" in line))
        self.manager.awaiting_approval.side_effect = lambda user, task_id: 0
        update, message = self.message_update()
        await tasks_command(update, self.context)
        self.assertNotIn("awaiting approval", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
