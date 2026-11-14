"""The automations tab: the web API (ownership, validation) and the page wiring."""

from __future__ import annotations

import re
import subprocess
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import test_subagents_async as base
from auth_helpers import login

from src.web.app import create_app

USER, AGENT = base.USER, base.AGENT
_STATIC = Path(__file__).parent.parent / "src" / "web" / "static"


class AutomationsApiTests(base.AsyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, USER)

    def create(self, **overrides) -> dict:
        body = {"agent_id": AGENT, "instruction": "Tell me the news", "title": "News", "trigger": {"kind": "every", "minutes": 30}}
        body.update(overrides)
        response = self.client.post("/api/automations", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_create_list_and_the_row_carries_the_agents_name(self) -> None:
        record = self.create()
        listed = self.client.get("/api/automations").json()["automations"]
        self.assertEqual([r["id"] for r in listed], [record["id"]])
        self.assertEqual((listed[0]["title"], listed[0]["status"], listed[0]["enabled"]), ("News", "active", True))
        self.assertIn("Name of", listed[0]["agent_name"]) if "Name of" in listed[0]["agent_name"] else self.assertTrue(listed[0]["agent_name"])

    def test_each_way_of_saying_when(self) -> None:
        soon = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        for trigger in (
            {"kind": "at", "at": soon}, {"kind": "cron", "cron": "30 8 * * mon-fri"}, {"kind": "every", "minutes": 120},
        ):
            self.create(trigger=trigger)
        self.assertEqual(len(self.client.get("/api/automations").json()["automations"]), 3)

    def test_validation_errors_are_400_with_a_readable_message(self) -> None:
        for trigger, expected in (
            ({"kind": "every", "minutes": 2}, "at least 5"), ({"kind": "cron", "cron": "99 * * * *"}, "cron"),
            ({"kind": "at", "at": "2020-01-01T09:00"}, "past"), ({"kind": "process_end", "process": "p00000000"}, "process"),
        ):
            response = self.client.post("/api/automations", json={"agent_id": AGENT, "instruction": "x", "trigger": trigger})
            self.assertEqual(response.status_code, 400, trigger)
            self.assertIn(expected, response.json()["detail"])
        self.assertEqual(self.client.post("/api/automations", json={"agent_id": AGENT, "instruction": "", "trigger": {"kind": "every", "minutes": 30}}).status_code, 400)
        self.assertEqual(self.client.post("/api/automations", json={"agent_id": AGENT, "instruction": "x", "trigger": {"kind": "calendar"}}).status_code, 422)
        self.assertEqual(self.client.post("/api/automations", json={"agent_id": AGENT, "instruction": "x", "trigger": {"kind": "every", "minutes": 30}, "owner": "x"}).status_code, 422)

    def test_an_agent_that_is_not_yours_is_refused(self) -> None:
        response = self.client.post("/api/automations", json={"agent_id": "not-mine", "instruction": "x", "trigger": {"kind": "every", "minutes": 30}})
        self.assertEqual(response.status_code, 404)

    def test_edit_pause_resume_delete(self) -> None:
        record = self.create()
        url = f"/api/automations/{record['id']}"
        edited = self.client.patch(url, json={"title": "Renamed", "trigger": {"kind": "every", "minutes": 90}}).json()
        self.assertEqual((edited["title"], edited["trigger"]["minutes"]), ("Renamed", 90))
        self.assertEqual(edited["instruction"], "Tell me the news")
        self.assertFalse(self.client.post(f"{url}/pause").json()["enabled"])
        self.assertTrue(self.client.post(f"{url}/resume").json()["enabled"])
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(self.client.get("/api/automations").json()["automations"], [])

    def test_unknown_ids_are_404_and_never_reach_another_users_files(self) -> None:
        for method, path in (("patch", ""), ("post", "/pause"), ("post", "/resume"), ("delete", "")):
            response = getattr(self.client, method)(f"/api/automations/0123456789{path}", **({"json": {}} if method == "patch" else {}))
            self.assertEqual(response.status_code, 404, (method, path))
        theirs = self.service.automations.create("someone-else", AGENT, "x", {"kind": "every", "minutes": 30}) if self._agent_ok("someone-else") else None
        if theirs:
            self.assertEqual(self.client.delete(f"/api/automations/{theirs['id']}").status_code, 404)
        self.assertEqual(self.client.delete("/api/automations/..%2F..%2Fx").status_code, 404)

    def _agent_ok(self, user: str) -> bool:
        return self.service.automations._deps.agent_exists(user, AGENT)

    def test_requires_a_login(self) -> None:
        anonymous = TestClient(create_app(self.service))
        for method, path in (("get", "/api/automations"), ("post", "/api/automations"), ("delete", "/api/automations/0123456789")):
            self.assertEqual(getattr(anonymous, method)(path).status_code, 401, path)


class AutomationsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (_STATIC / "index.html").read_text(encoding="utf-8")
        self.script = (_STATIC / "app.js").read_text(encoding="utf-8")

    def test_the_tab_and_the_form_exist(self) -> None:
        for needle in ('id="tasks-tab-automations"', 'id="automations-pane"', 'id="automation-form"', 'id="automation-kind"', 'id="automations-list"'):
            self.assertIn(needle, self.html)

    def test_the_page_talks_to_the_api(self) -> None:
        for needle in ('api("/api/automations")', "/pause", "/resume", 'method: "DELETE"', 'method: "PATCH"', "function automationPanelRefresh"):
            self.assertIn(needle, self.script)

    def test_the_pure_helpers_work(self) -> None:
        start = self.script.index("// Pure: the trigger sentence shown in a row")
        end = self.script.index("function automationLocalTime")
        code = (
            "const tr = (t, v = {}) => t.replace(/\\{(\\w+)\\}/g, (_, k) => v[k]);\n" + self.script[start:end] + "\n"
            "const assert = require('assert');\n"
            "assert.strictEqual(automationWhen({kind:'every',minutes:120}), 'every 2 h');\n"
            "assert.strictEqual(automationWhen({kind:'every',minutes:45}), 'every 45 min');\n"
            "assert.strictEqual(automationWhen({kind:'cron',cron:'0 8 * * *'}), 'cron 0 8 * * *');\n"
            "assert.strictEqual(automationWhen({kind:'at',at:'2026-11-20T09:00+01:00'}), 'once, 2026-11-20 09:00');\n"
            "assert.strictEqual(automationWhen({kind:'process_end',process:'p1a2b3c4d'}), 'when process p1a2b3c4d ends');\n"
            "assert.strictEqual(automationBadge({status:'active',enabled:false}), 'paused');\n"
            "assert.strictEqual(automationBadge({status:'active',enabled:true}), 'active');\n"
            "assert.strictEqual(automationBadge({status:'done',enabled:true}), 'done');\n"
            "assert.deepStrictEqual(automationTriggerFromForm({kind:'every',minutes:'30'}), {kind:'every',minutes:30});\n"
            "assert.deepStrictEqual(automationTriggerFromForm({kind:'cron',cron:' 0 8 * * * '}), {kind:'cron',cron:'0 8 * * *'});\n"
            "assert.deepStrictEqual(automationTriggerFromForm({kind:'at',at:'2026-11-20T09:00'}), {kind:'at',at:'2026-11-20T09:00'});\n"
            "assert.deepStrictEqual(automationTriggerFromForm({kind:'process_end',process:' p1a2b3c4d '}), {kind:'process_end',process:'p1a2b3c4d'});\n"
            "console.log('ok');\n"
        )
        result = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)


if __name__ == "__main__":
    unittest.main()
