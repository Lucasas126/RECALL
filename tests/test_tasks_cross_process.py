"""Phase 4 of the subagents feature: two ChatService instances (the web and the Telegram process) on one data dir.

The executor of a task is the process that runs the parent's turn; the other process asks through the commands
inbox (stop, steer, send), delivers the wake of chats of the origin it serves, and recovers tasks whose executor died.
Approvals are never relayed (decision 6): the other channel only says where to answer.
"""

from __future__ import annotations

import asyncio
import threading
import unittest
from datetime import datetime, timedelta
from time import sleep
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import test_subagents as sub
import test_subagents_async as asyn
from src.core.sources.conversations import ChatTask
from src.service.chat import ChatService
from src.telegram import bot

USER, AGENT = asyn.USER, asyn.AGENT
until = asyn.until


class CrossProcessFixture(asyn.AsyncFixture):
    """`self.service` is the web process (executor); `self.other` is a Telegram process on the same files."""

    def setUp(self) -> None:
        super().setUp()
        self.manager.origins = frozenset({"web"})
        self.other = ChatService(agent_factory=self._factory, origins={"telegram"})
        self.addCleanup(self._close, self.other)
        self.third = ChatService(agent_factory=self._factory, origins={"telegram"})  # a second Telegram-side poller
        self.addCleanup(self._close, self.third)
        for service in (self.other, self.third):
            service._tasks.wake_debounce = 0.05
        conversations = self.sources[(USER, AGENT)]
        conversations.append(self.chat, "user", "hello")  # chats with turns: creating the next one does not prune them
        conversations.append(self.chat, "assistant", "hi")
        self.tchat = self.service.create_chat(USER, AGENT, origin="telegram")["id"]
        conversations.append(self.tchat, "user", "hello")
        conversations.append(self.tchat, "assistant", "hi")
        self.child_behavior = "slow"

    def _close(self, service: ChatService) -> None:
        service.close()
        for slot in list(service._slots.values()):
            if slot.worker is not None:
                slot.worker.join(3)

    @property
    def remote(self):
        return self.other._tasks

    def finish(self, live) -> None:
        self.release_child.set()
        self.assertTrue(until(lambda: live.done.is_set()))

    def tele_wakes(self) -> list[dict]:
        return self.wakes(self.tchat)

    def tele_idle(self) -> bool:
        return not any(s.chat_active(USER, AGENT, self.tchat) for s in (self.service, self.other, self.third))


class InboxTests(CrossProcessFixture):
    def test_stop_from_the_other_process_is_queued_and_applied_by_the_executor(self) -> None:
        live = self.launch(wake=False)
        self.assertTrue(until(lambda: "RESULT for: find X" in "".join(live.partial)))
        out = self.remote.stop_task(USER, live.task_id)
        self.assertEqual(out["status"], "queued")
        self.assertIsNone(self.remote.command_outcome(USER, live.task_id, out["command"]))
        self.assertTrue(self.remote.store.command_path(USER, live.task_id).exists())
        self.assertEqual(self.manager.poll_commands(), 1)
        self.assertTrue(live.done.wait(8))
        self.assertEqual(self.record(live.task_id)["status"], "cancelled")
        outcome = self.remote.command_outcome(USER, live.task_id, out["command"])
        self.assertEqual((outcome["kind"], outcome["status"]), ("stop", "applied"))
        self.assertFalse(self.remote.store.command_path(USER, live.task_id).exists(), "a command is applied once")

    def test_steer_from_the_other_process_corrects_the_running_worker(self) -> None:
        live = self.launch(wake=False)
        self.assertTrue(until(lambda: "RESULT for: find X" in "".join(live.partial)))
        out = self.remote.steer(USER, live.task_id, "use source B")
        self.assertEqual(out["status"], "queued")
        self.manager.poll_commands()
        self.assertTrue(live.done.wait(8))
        record = self.record(live.task_id)
        self.assertEqual((record["status"], record["result"]["text"]), ("done", "CORRECTED: use source B"))
        self.assertEqual(self.remote.command_outcome(USER, live.task_id, out["command"])["status"], "applied")

    def test_a_send_to_a_running_task_is_rejected_with_the_reason(self) -> None:
        live = self.launch(wake=False)
        out = self.remote.send(USER, live.task_id, "more", limits=sub.LIMITS)
        self.assertEqual(out["status"], "queued")
        self.manager.poll_commands()
        outcome = self.remote.command_outcome(USER, live.task_id, out["command"])
        self.assertEqual(outcome["status"], "rejected")
        self.assertIn("task_steer", outcome["note"])
        self.finish(live)

    def test_send_for_a_finished_task_runs_in_any_process(self) -> None:
        self.child_behavior = "ok"
        live = self.launch(wake=False)
        self.assertTrue(live.done.wait(8))
        out = self.remote.send(USER, live.task_id, "dig deeper", limits=sub.LIMITS)
        self.assertEqual((out["status"], out["followup"]), ("running", 1))
        self.assertTrue(until(lambda: "follow-up dig deeper" in ((self.record(live.task_id).get("result") or {}).get("text") or "")))
        self.assertTrue(until(lambda: not self.remote._live))

    def test_commands_for_a_task_that_ended_are_answered_not_lost(self) -> None:
        self.child_behavior = "ok"
        live = self.launch(wake=False)
        self.assertTrue(live.done.wait(8))
        command = self.remote.store.enqueue(USER, live.task_id, "steer", "late")
        self.manager.apply_commands(USER, live.task_id, limits=sub.LIMITS)
        self.assertEqual(self.remote.command_outcome(USER, live.task_id, command)["status"], "rejected")

    def test_a_command_for_a_task_nobody_runs_is_an_error_not_a_queue(self) -> None:
        live = self.launch(wake=False)
        self.finish(live)
        self.remote.store.update(USER, live.task_id, lambda r: r.update(status="running", ended_at=None), final_ok=True)
        with self.assertRaisesRegex(sub.TaskError, "not running here"):
            self.remote.steer(USER, live.task_id, "x")  # its run lock is free: there is no executor to ask

    def test_the_poller_thread_applies_commands(self) -> None:
        self.manager.command_poll_seconds = 0.05
        self.manager.start_poller()
        self.addCleanup(self.manager.stop_poller)
        live = self.launch(wake=False)
        self.assertTrue(until(lambda: "RESULT for: find X" in "".join(live.partial)))
        self.remote.stop_task(USER, live.task_id)
        self.assertTrue(live.done.wait(8))
        self.assertEqual(self.record(live.task_id)["status"], "cancelled")
        self.manager.stop_poller()
        self.assertIsNone(self.manager._poller)

    def test_visibility_does_not_depend_on_the_process(self) -> None:
        live = self.launch(wake=False)
        views = self.other.list_tasks(USER, AGENT, self.chat)["tasks"]
        self.assertEqual([(v["id"], v["status"]) for v in views], [(live.task_id, "running")])
        self.finish(live)


class WakeClaimTests(CrossProcessFixture):
    def finished_for(self, chat: str):
        live = self.launch(parent=self.parent(chat), wake=True)
        self.finish(live)
        return live

    def test_the_executor_that_serves_the_origin_delivers_at_once(self) -> None:
        live = self.finished_for(self.chat)
        self.assertTrue(until(lambda: self.settled(1)))
        self.assertEqual(self.record(live.task_id)["wake"]["state"], "delivered")

    def test_the_origin_process_delivers_a_wake_when_the_executor_is_the_other_one(self) -> None:
        live = self.finished_for(self.tchat)
        sleep(0.5)  # longer than the debounce: the web executor must NOT deliver into a Telegram chat
        self.assertEqual(self.record(live.task_id)["wake"]["state"], "pending")
        self.assertEqual(self.tele_wakes(), [])
        self.assertEqual(self.manager.poll_wakes(), 0, "the web process does not serve telegram chats")
        self.assertEqual(self.remote.poll_wakes(), 1)
        self.assertTrue(until(lambda: len(self.tele_wakes()) == 1 and self.tele_idle()))
        self.assertEqual(self.record(live.task_id)["wake"]["state"], "delivered")
        self.assertIn("RESULT for: find X", self.tele_wakes()[0]["content"])

    def test_two_pollers_claim_a_wake_once(self) -> None:
        live = self.finished_for(self.tchat)
        barrier = threading.Barrier(2)

        def poll(service: ChatService) -> None:
            barrier.wait()
            service._tasks.poll_wakes()

        threads = [threading.Thread(target=poll, args=(s,)) for s in (self.other, self.third)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertTrue(until(lambda: len(self.tele_wakes()) >= 1 and self.tele_idle()))
        sleep(0.3)
        self.assertEqual(len(self.tele_wakes()), 1, "one delivery, whoever won the claim")
        self.assertEqual(self.record(live.task_id)["wake"]["state"], "delivered")

    def test_a_fresh_claim_is_left_alone_and_a_stale_one_is_taken_over(self) -> None:
        live = self.finished_for(self.tchat)

        def claim(age: int) -> None:
            at = (datetime.now().astimezone() - timedelta(seconds=age)).replace(microsecond=0).isoformat(timespec="seconds")
            self.remote.store.update(
                USER, live.task_id, lambda r: r.update(wake={"state": "claimed", "claimed_at": at, "claimed_by": "crashed-process"}), final_ok=True,
            )

        claim(5)
        self.assertEqual(self.remote.poll_wakes(), 0, "its claimant may still be delivering")
        self.assertEqual(self.tele_wakes(), [])
        claim(120)
        self.assertEqual(self.remote.poll_wakes(), 1)
        self.assertTrue(until(lambda: len(self.tele_wakes()) == 1 and self.tele_idle()))
        self.assertEqual(self.record(live.task_id)["wake"]["state"], "delivered")

    def test_the_claim_names_the_process_that_took_it(self) -> None:
        live = self.finished_for(self.tchat)
        claimed, _ = self.remote._claim(USER, AGENT, self.tchat, capped=False, seen=set())
        self.assertEqual([r["id"] for r in claimed], [live.task_id])
        wake = self.record(live.task_id)["wake"]
        self.assertEqual((wake["state"], wake["claimed_by"]), ("claimed", self.remote.process_id))
        self.assertNotEqual(self.remote.process_id, self.manager.process_id)

    def test_a_process_without_origins_claims_nothing(self) -> None:
        live = self.finished_for(self.tchat)
        self.other._tasks.origins = frozenset()
        self.assertEqual(self.other._tasks.poll_wakes(), 0)
        self.assertEqual(self.record(live.task_id)["wake"]["state"], "pending")


class RecoveryTests(CrossProcessFixture):
    def lost(self, chat: str, task_id: str = "eeeeeeeeeeee") -> str:
        self.manager.store.save({
            "id": task_id, "user_id": USER, "parent": {"agent_id": AGENT, "chat_id": chat},
            "child": {"agent_id": AGENT, "chat_id": None}, "status": "running", "title": "lost", "wake": {"state": "pending"},
            "created_at": "2026-11-05T10:00:00+01:00", "started_at": "2026-11-05T10:00:00+01:00", "usage": {"in": 0, "out": 0},
        })
        return task_id

    def test_the_poller_fails_a_task_whose_executor_died_and_its_origin_tells_the_parent(self) -> None:
        task_id = self.lost(self.tchat)
        self.remote.poll_wakes()  # not only at start-up
        dead = self.record(task_id)
        self.assertEqual((dead["status"], dead["reason"]), ("failed", "owner_lost"))
        self.assertTrue(until(lambda: len(self.tele_wakes()) == 1 and self.tele_idle()))
        self.assertIn("owner_lost", self.tele_wakes()[0]["content"])

    def test_a_process_that_does_not_serve_the_origin_fails_the_task_but_leaves_the_wake(self) -> None:
        task_id = self.lost(self.chat)  # a web chat
        self.remote.poll_wakes()
        self.assertEqual(self.record(task_id)["reason"], "owner_lost")
        sleep(0.3)
        self.assertEqual(self.wakes(), [])
        self.assertEqual(self.record(task_id)["wake"]["state"], "pending")
        self.manager.poll_wakes()  # the web process delivers
        self.assertTrue(until(lambda: self.settled(1)))

    def test_a_live_task_is_not_failed_by_the_other_processs_poller(self) -> None:
        live = self.launch(wake=False)
        self.remote.poll_wakes()
        self.assertEqual(self.record(live.task_id)["status"], "running")
        self.finish(live)


class ApprovalsAreNotRelayedTests(CrossProcessFixture):
    def mark(self, task_id: str) -> ChatTask:
        return ChatTask(
            task_id=task_id, parent_agent_id=AGENT, parent_chat_id=self.chat, groups=[], instructions="",
            created_at="2026-11-05T10:00:00+01:00",
        )

    def test_the_other_channel_shows_where_to_answer_and_never_relays_the_card(self) -> None:
        live = self.launch(wake=False)
        context = self.manager.bind_worker(self.parent(), self.mark(live.task_id))
        context.approval_wait(True)
        record = self.record(live.task_id)
        self.assertEqual((record["awaiting_approval"], record["approval_channel"]), (1, "web"))
        remote = self.remote.view(record)
        self.assertEqual((remote["awaiting_approval"], remote["approval_elsewhere"]), (1, "web"))
        self.assertIsNone(self.manager.view(record)["approval_elsewhere"], "the executor's own channel shows the card itself")
        self.assertEqual(self.other.list_approvals(USER, AGENT, self.chat), [], "the other process has no card to answer")
        context.approval_wait(False)
        self.assertEqual(self.remote.view(self.record(live.task_id))["awaiting_approval"], 0)
        self.finish(live)

    def test_the_telegram_task_list_says_where_to_answer(self) -> None:
        live = self.launch(wake=False)
        context = self.manager.bind_worker(self.parent(), self.mark(live.task_id))
        context.approval_wait(True)
        state = SimpleNamespace(service=self.other, task_cards={})
        session = SimpleNamespace(user_id=USER, agent_id=AGENT, chat_id=self.chat, language="en")
        reply = AsyncMock()
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))
        telegram_context = SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}))
        with patch.object(bot, "_session", return_value=session):
            asyncio.run(bot.tasks_command(update, telegram_context))
        self.assertIn("awaiting approval (answer it in Web)", reply.call_args.args[0])
        context.approval_wait(False)
        self.finish(live)


if __name__ == "__main__":
    unittest.main()
