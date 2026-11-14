"""The automations tool group: what an agent can schedule, and what it cannot touch."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_TOOLS
from src.agent.toolsets.automations import build_automation_tools
from src.config import ceiling
from src.config.levels import Level
from src.service.automations import AutomationDependencies, AutomationService
from src.terminal.context import RunContext, run_context

MADRID = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 14, 8, 0, tzinfo=MADRID)


class ToolFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.service = AutomationService(Path(directory.name), AutomationDependencies(
            launch=Mock(), agent_exists=lambda user, agent: agent in ("bot", "other"), clock=lambda: NOW,
            process_state=lambda user, pid: "running" if pid == "p1a2b3c4d" else None,
        ))
        self.tools = {f.tool_spec["function"]["name"]: f for f in build_automation_tools(self.config("user-a", "bot"))}

    @staticmethod
    def config(user: str, agent: str):
        return SimpleNamespace(user_id=user, id=agent)

    def context(self, user="user-a", agent="bot", *, task=None, automations=True) -> RunContext:
        return RunContext(
            user, agent, "chat", Mock(), lambda *_: None, task=task,
            automations=self.service if automations else None,
        )

    def call(self, name: str, *, user="user-a", agent="bot", task=None, automations=True, **kwargs) -> str:
        with run_context(self.context(user, agent, task=task, automations=automations)):
            return self.tools[name](**kwargs)

    def payload(self, text: str) -> dict:
        return json.loads(text.split(": ", 1)[1])


class CreateTests(ToolFixture):
    def test_each_way_of_saying_when(self) -> None:
        cases = [
            dict(at="2026-11-20T09:00"), dict(every_minutes=90), dict(cron="30 8 * * mon-fri"), dict(after_process="p1a2b3c4d"),
        ]
        for when in cases:
            result = self.call("automation_create", instruction="Tell me the news", title="News", **when)
            self.assertTrue(result.startswith("scheduled: "), result)
        self.assertEqual(len(self.service.list("user-a", "bot")), 4)

    def test_the_summary_says_when_and_the_owner_is_the_run_user(self) -> None:
        result = self.payload(self.call("automation_create", instruction="x", every_minutes=120))
        self.assertEqual((result["when"], result["status"], result["enabled"]), ("every 2 h", "active", True))
        record = self.service.store.get("user-a", result["id"])
        self.assertEqual((record["user_id"], record["agent_id"], record["created_by"]), ("user-a", "bot", "bot"))

    def test_exactly_one_when(self) -> None:
        for kwargs in ({}, dict(every_minutes=10, cron="0 8 * * *"), dict(at="2026-11-20T09:00", after_process="p1a2b3c4d")):
            self.assertIn("exactly one", self.call("automation_create", instruction="x", **kwargs), kwargs)
        self.assertEqual(self.service.list("user-a"), [])

    def test_service_errors_come_back_as_readable_text(self) -> None:
        for kwargs, expected in (
            (dict(every_minutes=2), "at least 5"), (dict(at="2020-01-01T09:00"), "in the past"),
            (dict(cron="99 * * * *"), "cron"), (dict(after_process="p00000000"), "no background process"),
            (dict(instruction="   ", every_minutes=10), "instruction is empty"),
        ):
            kwargs.setdefault("instruction", "x")
            result = self.call("automation_create", **kwargs)
            self.assertTrue(result.startswith("error: "), result)
            self.assertIn(expected, result)

    def test_the_tool_never_takes_an_owner_or_an_agent(self) -> None:
        for function in self.tools.values():
            self.assertFalse({"user", "user_id", "agent", "agent_id"} & set(function.tool_spec["function"]["parameters"]["properties"]))


class ManageTests(ToolFixture):
    def make(self, **kwargs) -> str:
        return self.payload(self.call("automation_create", instruction="x", title="T", every_minutes=30, **kwargs))["id"]

    def test_list_pause_resume_edit_delete(self) -> None:
        self.assertEqual(self.call("automation_list"), "no automations")
        automation_id = self.make()
        listed = json.loads(self.call("automation_list"))
        self.assertEqual([r["id"] for r in listed], [automation_id])
        self.assertTrue(self.call("automation_pause", automation_id=automation_id).startswith("paused: "))
        self.assertFalse(json.loads(self.call("automation_list"))[0]["enabled"])
        self.assertTrue(self.call("automation_resume", automation_id=automation_id).startswith("resumed: "))
        edited = self.payload(self.call("automation_edit", automation_id=automation_id, title="Renamed", cron="0 9 * * *"))
        self.assertEqual((edited["title"], edited["when"]), ("Renamed", "cron '0 9 * * *'"))
        self.assertEqual(self.service.store.get("user-a", automation_id)["instruction"], "x")  # empty keeps it
        self.assertEqual(self.call("automation_delete", automation_id=automation_id), f"deleted {automation_id}")
        self.assertEqual(self.call("automation_list"), "no automations")

    def test_an_agent_only_sees_and_touches_its_own(self) -> None:
        mine = self.make()
        other = self.service.create("user-a", "other", "theirs", {"kind": "every", "minutes": 30})["id"]
        self.assertEqual([r["id"] for r in json.loads(self.call("automation_list"))], [mine])
        for name in ("automation_pause", "automation_resume", "automation_delete"):
            self.assertIn("unknown automation", self.call(name, automation_id=other))
        self.assertIn("unknown automation", self.call("automation_edit", automation_id=other, title="mine now"))
        self.assertEqual(self.service.store.get("user-a", other)["title"], "theirs")

    def test_a_bad_id_is_an_error_not_a_crash(self) -> None:
        for name in ("automation_pause", "automation_delete"):
            self.assertIn("unknown automation", self.call(name, automation_id="../../x"))
            self.assertIn("unknown automation", self.call(name, automation_id=""))


class ContextTests(ToolFixture):
    def test_refused_without_the_service_or_for_another_run(self) -> None:
        for kwargs in (dict(automations=False), dict(user="user-b"), dict(agent="other")):
            self.assertIn("not available", self.call("automation_list", **kwargs), kwargs)
        self.assertIn("not available", self.tools["automation_list"]())  # no run at all

    def test_a_task_run_cannot_schedule_more(self) -> None:
        self.assertIn("not available", self.call("automation_create", instruction="x", every_minutes=30, task={"id": "t"}))
        self.assertEqual(self.service.list("user-a"), [])

    def test_another_users_automations_are_out_of_reach(self) -> None:
        theirs = self.service.create("user-b", "bot", "x", {"kind": "every", "minutes": 30})["id"]
        self.assertEqual(self.call("automation_list"), "no automations")
        self.assertIn("unknown automation", self.call("automation_delete", automation_id=theirs))
        self.assertIsNotNone(self.service.store.get("user-b", theirs))


class GroupTests(unittest.TestCase):
    def test_level_and_disposable_chats(self) -> None:
        self.assertEqual(ceiling.GROUP_MIN_LEVEL["automations"], Level.ASSISTANT)
        self.assertFalse(ceiling.tool_allowed(Level.READER, "automation_create"))
        self.assertTrue(ceiling.tool_allowed(Level.ASSISTANT, "automation_create"))
        self.assertLessEqual(set(TOOL_GROUP_TOOLS["automations"]), DISPOSABLE_BLOCKED_TOOLS)
        self.assertEqual(len(TOOL_GROUP_TOOLS["automations"]), 6)


if __name__ == "__main__":
    unittest.main()
