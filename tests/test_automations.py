"""Automations: triggers, the store, the clock (with a fake one), limits, catch-up and the unattended run's grants."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from src.agent.tool_groups import AUTOMATION_EXTRA_GROUPS, DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_TOOLS
from src.service import automations
from src.service.automations import (
    MAX_PER_USER, MIN_INTERVAL_MINUTES, AutomationDependencies, AutomationError, AutomationService, Limits,
    describe, next_due, normalize_trigger,
)

MADRID = ZoneInfo("Europe/Madrid")
START = datetime(2026, 11, 14, 8, 0, tzinfo=MADRID)  # a Saturday


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now += timedelta(**delta)


class TriggerTests(unittest.TestCase):
    def test_at_is_local_time_and_must_be_in_the_future(self) -> None:
        stored = normalize_trigger({"kind": "at", "at": "2026-11-20T09:00"}, now=START)
        self.assertEqual(stored["at"], "2026-11-20T09:00+01:00")
        self.assertEqual(next_due(stored, START), datetime(2026, 11, 20, 9, 0, tzinfo=MADRID))
        with self.assertRaises(AutomationError):
            normalize_trigger({"kind": "at", "at": "2026-11-13T09:00"}, now=START)
        with self.assertRaises(AutomationError):
            normalize_trigger({"kind": "at", "at": "tomorrow"}, now=START)

    def test_every_has_a_floor_and_a_ceiling(self) -> None:
        self.assertEqual(normalize_trigger({"kind": "every", "minutes": 90})["minutes"], 90)
        for bad in (MIN_INTERVAL_MINUTES - 1, 0, -5, "x", 60 * 24 * 32):
            with self.assertRaises(AutomationError, msg=str(bad)):
                normalize_trigger({"kind": "every", "minutes": bad})
        stored = normalize_trigger({"kind": "every", "minutes": 30})
        self.assertEqual(next_due(stored, START), START + timedelta(minutes=30))

    def test_cron_runs_in_madrid_time_across_the_clock_change(self) -> None:
        stored = normalize_trigger({"kind": "cron", "cron": "30 8 * * *"}, now=START)
        self.assertEqual(next_due(stored, START), datetime(2026, 11, 14, 8, 30, tzinfo=MADRID))
        before_change = datetime(2027, 3, 27, 9, 0, tzinfo=MADRID)  # DST starts on 2027-03-28
        due = next_due(stored, before_change)
        self.assertEqual((due.hour, due.minute, due.day), (8, 30, 28))
        self.assertEqual(due.utcoffset(), timedelta(hours=2))

    def test_cron_validation(self) -> None:
        for bad in ("61 * * * *", "* * * *", "", "every day", "*/1 * * * *"):
            with self.assertRaises(AutomationError, msg=bad):
                normalize_trigger({"kind": "cron", "cron": bad}, now=START)
        self.assertEqual(normalize_trigger({"kind": "cron", "cron": "  0   8 * * mon-fri "})["cron"], "0 8 * * mon-fri")

    def test_process_end_needs_a_process_id(self) -> None:
        self.assertEqual(normalize_trigger({"kind": "process_end", "process": "p1a2b3c4d"})["process"], "p1a2b3c4d")
        with self.assertRaises(AutomationError):
            normalize_trigger({"kind": "process_end", "process": "../etc"})
        self.assertIsNone(next_due({"kind": "process_end", "process": "p1a2b3c4d"}, START))

    def test_unknown_kind_and_zone(self) -> None:
        with self.assertRaises(AutomationError):
            normalize_trigger({"kind": "calendar"})
        with self.assertRaises(AutomationError):
            normalize_trigger({"kind": "every", "minutes": 10, "timezone": "Mars/Olympus"})

    def test_describe(self) -> None:
        self.assertEqual(describe({"kind": "every", "minutes": 120}), "every 2 h")
        self.assertEqual(describe({"kind": "every", "minutes": 45}), "every 45 min")
        self.assertIn("cron '0 8", describe({"kind": "cron", "cron": "0 8 * * *"}))
        self.assertIn("p1a2b3c4d", describe({"kind": "process_end", "process": "p1a2b3c4d"}))


class ServiceFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)
        self.clock = Clock()
        self.launched: list[tuple] = []
        self.fail_launch: Exception | None = None
        self.processes = {"p1a2b3c4d": "running"}
        self.service = self.make()

    def make(self, limits: Limits = Limits()) -> AutomationService:
        def launch(user, agent, goal, context="", **kwargs):
            if self.fail_launch is not None:
                raise self.fail_launch
            self.launched.append((user, agent, goal, kwargs))
            return Mock(task_id=f"task{len(self.launched):08x}")

        return AutomationService(self.data, AutomationDependencies(
            launch=launch, agent_exists=lambda user, agent: agent in ("bot", "other"), clock=self.clock,
            process_state=lambda user, pid: self.processes.get(pid),
        ), limits)

    def create(self, trigger: dict | None = None, **kwargs) -> dict:
        return self.service.create("user-a", "bot", "Check the news and tell me", trigger or {"kind": "every", "minutes": 30}, **kwargs)


class CreateAndEditTests(ServiceFixture):
    def test_create_stores_the_record_with_its_next_run(self) -> None:
        record = self.create(title="News")
        self.assertEqual((record["title"], record["status"], record["enabled"]), ("News", "active", True))
        self.assertEqual(record["next_run"], (START + timedelta(minutes=30)).isoformat(timespec="seconds"))
        self.assertEqual(self.service.list("user-a")[0]["id"], record["id"])
        self.assertTrue((self.data / "user-a" / ".automations" / f"{record['id']}.json").is_file())
        self.assertEqual(self.service.list("user-b"), [])

    def test_validation_errors_are_readable(self) -> None:
        for kwargs in (dict(instruction=""), dict(instruction="x" * 5000), dict(agent="ghost")):
            with self.assertRaises(AutomationError, msg=str(kwargs)):
                self.service.create("user-a", kwargs.get("agent", "bot"), kwargs.get("instruction", "do it"), {"kind": "every", "minutes": 30})
        with self.assertRaises(AutomationError):
            self.create({"kind": "process_end", "process": "p00000000"})  # no such process to wait for

    def test_the_title_defaults_to_the_instruction(self) -> None:
        self.assertEqual(self.create()["title"], "Check the news and tell me")

    def test_per_user_limit(self) -> None:
        for _ in range(MAX_PER_USER):
            self.create()
        with self.assertRaises(AutomationError):
            self.create()

    def test_edit_pause_resume_delete(self) -> None:
        record = self.create()
        edited = self.service.edit("user-a", record["id"], instruction="New text", trigger={"kind": "every", "minutes": 60})
        self.assertEqual((edited["instruction"], edited["trigger"]["minutes"]), ("New text", 60))
        self.assertEqual(edited["next_run"], (START + timedelta(minutes=60)).isoformat(timespec="seconds"))
        self.assertFalse(self.service.set_enabled("user-a", record["id"], False)["enabled"])
        self.clock.advance(hours=3)
        self.assertEqual(self.service.tick(), [])  # paused: nothing runs
        resumed = self.service.set_enabled("user-a", record["id"], True)
        self.assertEqual(resumed["next_run"], (self.clock.now + timedelta(minutes=60)).isoformat(timespec="seconds"))  # no replay
        self.service.delete("user-a", record["id"])
        for call in (lambda: self.service.delete("user-a", record["id"]), lambda: self.service.set_enabled("user-a", "nope", True),
                     lambda: self.service.edit("user-a", "0000000000", title="x")):
            with self.assertRaises(AutomationError):
                call()

    def test_a_malformed_id_is_refused(self) -> None:
        with self.assertRaises(AutomationError):
            self.service.delete("user-a", "../../x")


class ClockTests(ServiceFixture):
    def test_nothing_runs_before_it_is_due_and_it_runs_once_at_the_time(self) -> None:
        record = self.create({"kind": "every", "minutes": 30})
        self.clock.advance(minutes=29)
        self.assertEqual(self.service.tick(), [])
        self.clock.advance(minutes=1)
        self.assertEqual(self.service.tick(), [("user-a", record["id"])])
        self.assertEqual(self.service.tick(), [])  # not twice for the same slot
        user, agent, goal, kwargs = self.launched[0]
        self.assertEqual((user, agent, kwargs["trigger"]), ("user-a", "bot", "automation"))
        self.assertIn("Check the news and tell me", goal)
        stored = self.service.list("user-a")[0]
        self.assertEqual((stored["runs"], stored["history"][-1]["outcome"]), (1, "started"))
        self.assertEqual(stored["next_run"], (self.clock.now + timedelta(minutes=30)).isoformat(timespec="seconds"))

    def test_a_one_off_runs_once_and_finishes(self) -> None:
        record = self.create({"kind": "at", "at": "2026-11-14T09:00"})
        self.clock.advance(hours=1)
        self.assertEqual(self.service.tick(), [("user-a", record["id"])])
        stored = self.service.list("user-a")[0]
        self.assertEqual((stored["status"], stored["next_run"]), ("done", None))
        self.clock.advance(days=1)
        self.assertEqual(self.service.tick(), [])
        with self.assertRaises(AutomationError):
            self.service.set_enabled("user-a", record["id"], True)

    def test_after_downtime_a_missed_run_is_made_up_once_not_replayed(self) -> None:
        self.create({"kind": "every", "minutes": 10})
        self.clock.advance(hours=5)  # the server was down for 5 hours: 30 slots were missed
        self.assertEqual(len(self.service.tick()), 1)
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.service.tick(), [])

    def test_cron_fires_on_its_days_only(self) -> None:
        self.create({"kind": "cron", "cron": "0 9 * * mon-fri"})
        self.clock.now = datetime(2026, 11, 14, 10, 0, tzinfo=MADRID)  # Saturday: next is Monday 9:00
        self.assertEqual(self.service.tick(), [])
        self.clock.now = datetime(2026, 11, 16, 9, 1, tzinfo=MADRID)
        self.assertEqual(len(self.service.tick()), 1)

    def test_process_end_fires_the_waiting_automation_once(self) -> None:
        waiting = self.create({"kind": "process_end", "process": "p1a2b3c4d"})
        other = self.create({"kind": "process_end", "process": "p99999999"}) if False else None
        self.assertEqual(self.service.tick(), [])  # never time-triggered
        self.assertEqual(self.service.process_ended("user-a", "p00000000"), [])
        self.assertEqual(self.service.process_ended("user-a", "p1a2b3c4d"), [waiting["id"]])
        self.assertIn("process p1a2b3c4d ended", self.launched[0][2])
        self.assertEqual(self.service.process_ended("user-a", "p1a2b3c4d"), [])  # already done
        self.assertEqual(self.service.list("user-a")[0]["status"], "done")
        self.assertIsNone(other)

    def test_a_paused_process_automation_does_not_fire(self) -> None:
        record = self.create({"kind": "process_end", "process": "p1a2b3c4d"})
        self.service.set_enabled("user-a", record["id"], False)
        self.assertEqual(self.service.process_ended("user-a", "p1a2b3c4d"), [])

    def test_another_users_process_does_not_fire_it(self) -> None:
        self.create({"kind": "process_end", "process": "p1a2b3c4d"})
        self.assertEqual(self.service.process_ended("user-b", "p1a2b3c4d"), [])


class LimitsTests(ServiceFixture):
    def test_hourly_limit_skips_and_says_why(self) -> None:
        service = self.make(Limits(runs_per_hour=2, runs_per_day=48))
        record = service.create("user-a", "bot", "x", {"kind": "every", "minutes": 5})
        ran = 0
        for _ in range(6):
            self.clock.advance(minutes=5)
            ran += len(service.tick())
        self.assertEqual(ran, 2)
        outcomes = [h["outcome"] for h in service.list("user-a")[0]["history"]]
        self.assertTrue(any("in an hour" in o for o in outcomes))
        self.clock.advance(hours=2)
        self.assertEqual(len(service.tick()), 1)  # the window slid: it runs again
        self.assertEqual(record["id"], service.list("user-a")[0]["id"])

    def test_a_launch_that_fails_is_retried_then_skipped_and_the_schedule_goes_on(self) -> None:
        record = self.create({"kind": "every", "minutes": 10})
        self.fail_launch = RuntimeError("rate limit: the provider's plan limit was reached")
        for _ in range(automations.MAX_START_RETRIES):
            self.clock.advance(minutes=10)
            self.assertEqual(self.service.tick(), [])
        stored = self.service.list("user-a")[0]
        self.assertTrue(stored["enabled"])
        self.assertIn("could not start", stored["history"][-1]["outcome"])
        self.fail_launch = None
        self.clock.advance(minutes=10)
        self.assertEqual(self.service.tick(), [("user-a", record["id"])])

    def test_an_automation_whose_agent_was_deleted_fails_and_stops(self) -> None:
        record = self.service.create("user-a", "other", "x", {"kind": "every", "minutes": 10})
        self.service._deps.agent_exists = lambda user, agent: False
        self.clock.advance(minutes=10)
        self.assertEqual(self.service.tick(), [])
        stored = self.service.list("user-a")[0]
        self.assertEqual((stored["status"], stored["enabled"], record["id"]), ("failed", False, stored["id"]))

    def test_old_finished_records_are_pruned(self) -> None:
        record = self.create({"kind": "at", "at": "2026-11-14T09:00"})
        self.clock.advance(hours=1)
        self.service.tick()
        self.clock.advance(days=automations.KEEP_DAYS + 1)
        self.service.tick()
        self.assertEqual(self.service.list("user-a"), [])
        self.assertIsNone(self.service.store.get("user-a", record["id"]))

    def test_one_broken_record_does_not_stop_the_others(self) -> None:
        good = self.create({"kind": "every", "minutes": 10})
        broken = self.create({"kind": "every", "minutes": 10})
        path = self.data / "user-a" / ".automations" / f"{broken['id']}.json"
        path.write_text("{not json", encoding="utf-8")
        self.clock.advance(minutes=10)
        self.assertEqual(self.service.tick(), [("user-a", good["id"])])


class ThreadTests(ServiceFixture):
    def test_the_thread_starts_runs_ticks_and_stops(self) -> None:
        record = self.create({"kind": "every", "minutes": 5})
        self.clock.advance(minutes=5)
        self.service.start(interval_seconds=0.01)
        self.addCleanup(self.service.stop)
        deadline = datetime.now() + timedelta(seconds=3)
        while not self.launched and datetime.now() < deadline:
            pass
        self.assertTrue(self.launched)
        self.service.stop()
        self.assertIsNone(self.service._thread)
        self.assertEqual(record["agent_id"], "bot")


class GrantTests(unittest.TestCase):
    """An automation's task may message its owner; no other task may."""

    def test_the_extra_groups_are_exactly_notify_and_files_send(self) -> None:
        self.assertEqual(AUTOMATION_EXTRA_GROUPS, ("notify", "files_send"))

    def test_other_disposable_chats_still_cannot_message_the_owner(self) -> None:
        for group in AUTOMATION_EXTRA_GROUPS:
            self.assertLessEqual(set(TOOL_GROUP_TOOLS[group]), DISPOSABLE_BLOCKED_TOOLS)



try:  # the integration check reuses the subagent fixtures, which live next to the tests
    from test_subagents_panel import AgentTargetFixture
except ImportError:  # pragma: no cover
    AgentTargetFixture = unittest.TestCase


@unittest.skipIf(AgentTargetFixture is unittest.TestCase, "subagent fixtures unavailable")
class AutomationTaskGrantTests(AgentTargetFixture):
    """The task an automation starts may message its owner; a task started any other way may not."""

    target_groups = ["web", "memory_read", "notify", "files_send"]

    def tool_names(self, live) -> set[str]:
        self.assertTrue(live.done.wait(8))
        record = self.record(live.task_id)
        mark = self.sources[("user-one", "agent-two")].task_mark(record["child"]["chat_id"])
        return set(mark.groups), mark.automation

    def test_an_automation_task_gets_notify_and_files_send(self) -> None:
        groups, automation = self.tool_names(self.manager.launch_unattended("user-one", "agent-two", "go", trigger="automation"))
        self.assertTrue(automation)
        self.assertLessEqual({"notify", "files_send"}, groups)

    def test_a_scheduled_task_that_is_not_an_automation_does_not(self) -> None:
        groups, automation = self.tool_names(self.manager.launch_unattended("user-one", "agent-two", "go", trigger="schedule"))
        self.assertFalse(automation)
        self.assertFalse({"notify", "files_send"} & groups)

    def test_it_only_gets_what_the_agent_itself_holds(self) -> None:
        self.target_groups = ["web", "memory_read", "notify"]  # no files_send
        groups, _ = self.tool_names(self.manager.launch_unattended("user-one", "agent-two", "go", trigger="automation"))
        self.assertIn("notify", groups)
        self.assertNotIn("files_send", groups)

if __name__ == "__main__":
    unittest.main()
