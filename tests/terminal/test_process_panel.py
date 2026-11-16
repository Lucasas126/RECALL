"""The web panel of background processes: what the API lists and stops, and the static wiring of the page."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


from fastapi.testclient import TestClient  # noqa: E402

from tests.tasks import test_subagents_async as base  # noqa: E402
from tests.support.auth_helpers import login  # noqa: E402

from src.agent.toolsets.processes import process_registry  # noqa: E402
from src.terminal.backends import LocalBackend  # noqa: E402
from src.web.app import create_app  # noqa: E402

USER, AGENT = base.USER, base.AGENT
_STATIC = Path(__file__).parent.parent.parent / "src" / "web" / "static"


class ProcessPanelApiTests(base.AsyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, USER)
        self.registry = process_registry(self.data)

    def start(self, command: str, chat: str | None = None, notify: bool = False) -> str:  # type: ignore[override]
        chat = chat or self.chat
        self.addCleanup(lambda: self.registry.stop_chat(USER, chat))
        record = self.registry.start_local(
            user_id=USER, agent_id=AGENT, chat_id=chat, backend="box", command=command,
            argv=[*LocalBackend.shell(), command], cwd=str(self.data), env={"PATH": "/usr/bin:/bin"}, notify=notify,
        )
        return record["id"]

    def url(self, suffix: str = "", chat: str | None = None) -> str:
        return f"/api/agents/{AGENT}/chats/{chat or self.chat}/processes{suffix}"

    def test_lists_only_the_running_processes_of_this_chat(self) -> None:
        running = self.start("sleep 30")
        done = self.start("true")
        self.registry.wait(USER, done, 10)
        list(self.service.send(USER, AGENT, self.chat, "hello"))  # an empty chat is dropped when the next one is created
        other = self.service.create_chat(USER, AGENT)["id"]
        self.start("sleep 30", chat=other)
        listed = self.client.get(self.url()).json()["processes"]
        self.assertEqual([p["id"] for p in listed], [running])
        self.assertEqual(listed[0]["command"], "sleep 30")
        self.assertEqual(listed[0]["backend"], "box")
        self.assertIsInstance(listed[0]["started"], float)

    def test_the_command_is_redacted_before_it_reaches_the_page(self) -> None:
        self.start("sleep 30 # token sk-or-v1-" + "a" * 40)
        command = self.client.get(self.url()).json()["processes"][0]["command"]
        self.assertNotIn("a" * 40, command)

    def test_stop_ends_the_process_and_it_disappears_from_the_list(self) -> None:
        pid = self.start("sleep 30")
        response = self.client.post(self.url(f"/{pid}/stop"))
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.registry.get(USER, pid)["state"], "stopped")
        self.assertEqual(self.client.get(self.url()).json()["processes"], [])

    def test_stop_refuses_a_process_of_another_chat_or_an_unknown_one(self) -> None:
        list(self.service.send(USER, AGENT, self.chat, "hello"))
        other = self.service.create_chat(USER, AGENT)["id"]
        pid = self.start("sleep 30", chat=other)
        self.assertGreaterEqual(self.client.post(self.url(f"/{pid}/stop")).status_code, 400)
        self.assertEqual(self.registry.get(USER, pid)["state"], "running")
        self.assertGreaterEqual(self.client.post(self.url("/p00000000/stop")).status_code, 400)

    def test_unknown_chat_is_a_404(self) -> None:
        self.assertEqual(self.client.get(self.url(chat="20200101-000000-000000")).status_code, 404)

    def test_requires_a_login(self) -> None:
        anonymous = TestClient(create_app(self.service))
        self.assertEqual(anonymous.get(self.url()).status_code, 401)


class ProcessPanelStaticTests(unittest.TestCase):
    def test_panel_sits_above_the_box_and_debug_info_below_it(self) -> None:
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        panel, row, usage = html.index('id="processes"'), html.index('id="composer-row"'), html.index('id="usage"')
        self.assertLess(panel, row)
        self.assertGreater(usage, row)
        self.assertLess(usage, html.index('id="agents-view"'))

    def test_page_polls_the_processes_of_the_open_chat(self) -> None:
        script = (_STATIC / "app.js").read_text(encoding="utf-8")
        for needle in ("function pollProcesses", "/processes`", "startProcessPolling();", "/stop`"):
            self.assertIn(needle, script)


if __name__ == "__main__":
    unittest.main()
