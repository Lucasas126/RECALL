from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from pydantic import ValidationError

from src.config.models import McpSettings
from src.providers.mcp import McpBusy, McpManager, McpNotConnected, UnknownMcpServer
from src.service.chat import ChatService
from tests.config.test_user_preferences import UserPreferencesWebTests
from tests.providers.test_mcp import _settings
from tests.support.auth_helpers import login


class McpRefreshSettingsTests(unittest.TestCase):
    def test_default_is_daily_and_zero_disables(self) -> None:
        self.assertEqual(McpSettings().refresh_hours, 24)
        self.assertEqual(McpSettings(refresh_hours=0).refresh_hours, 0)
        self.assertEqual(McpSettings(refresh_hours=0.5).refresh_hours, 0.5)

    def test_negative_or_non_finite_is_rejected(self) -> None:
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                McpSettings(refresh_hours=value)


class McpRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        env = patch.dict(os.environ, {"RECALL_TEST_SHARED": "shared"})
        env.start()
        self.addCleanup(env.stop)
        self.manager = McpManager(_settings(), self.root)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.manager.close)

    def _connect(self, user_id: str, *flags: str) -> Path:
        folder = self.root / user_id / "mcp" / "fake"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "tokens.json").write_text("{}", encoding="utf-8")
        for flag in flags:
            (folder / flag).write_text("", encoding="utf-8")
        return folder

    def _names(self, user_id: str) -> set[str]:
        return {tool.name for tool in self.manager.list_tools(user_id, "fake")}

    def test_tool_list_is_kept_until_refresh_picks_up_a_new_tool(self) -> None:
        folder = self._connect("user-one")
        self.assertNotIn("late", self._names("user-one"))
        (folder / "late.flag").write_text("", encoding="utf-8")

        self.assertNotIn("late", self._names("user-one"))
        status = self.manager.refresh("user-one", "fake")

        self.assertTrue(status["connected"])
        self.assertIn("late", self._names("user-one"))
        self.assertEqual(self.manager.call_tool("user-one", "fake", "late", {}), "late")

    def test_refresh_errors_surface_for_unknown_and_not_connected(self) -> None:
        with self.assertRaises(UnknownMcpServer):
            self.manager.refresh("user-one", "missing")
        with self.assertRaises(McpNotConnected):
            self.manager.refresh("user-one", "fake")

    def test_refresh_of_a_server_not_yet_open_connects_it(self) -> None:
        self._connect("user-one")
        self.manager.refresh("user-one", "fake")
        self.assertIn(("user-one", "fake"), self.manager._connections)

    def test_busy_connection_is_not_restarted(self) -> None:
        self._connect("user-one", "slow.flag")
        self.manager.list_tools("user-one", "fake")
        connection = self.manager._connections[("user-one", "fake")]
        result: list[str] = []
        worker = threading.Thread(
            target=lambda: result.append(self.manager.call_tool("user-one", "fake", "slow", {"seconds": 2})),
        )
        worker.start()
        deadline = time.monotonic() + 10
        while not connection.busy and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(connection.busy)

        with self.assertRaises(McpBusy):
            self.manager.refresh("user-one", "fake")
        self.assertEqual(self.manager.refresh_idle(), 0)
        worker.join(30)

        self.assertEqual(result, ["done"])
        self.assertIs(self.manager._connections[("user-one", "fake")], connection)
        self.assertFalse(connection.busy)
        self.manager.refresh("user-one", "fake")  # idle again

    def test_a_failed_call_releases_the_connection(self) -> None:
        self._connect("user-one")
        with self.assertRaises(Exception):
            self.manager.call_tool("user-one", "fake", "fail", {})
        self.assertFalse(self.manager._connections[("user-one", "fake")].busy)

    def test_refresh_idle_only_touches_idle_connections(self) -> None:
        first = self._connect("user-one", "slow.flag")
        second = self._connect("user-two")
        self.manager.list_tools("user-one", "fake")
        self.manager.list_tools("user-two", "fake")
        (second / "late.flag").write_text("", encoding="utf-8")
        busy_connection = self.manager._connections[("user-one", "fake")]
        idle_connection = self.manager._connections[("user-two", "fake")]
        busy_connection.claim()
        try:
            self.assertEqual(self.manager.refresh_idle(), 1)
        finally:
            busy_connection.release()

        self.assertIs(self.manager._connections[("user-one", "fake")], busy_connection)
        self.assertIsNot(self.manager._connections[("user-two", "fake")], idle_connection)
        self.assertIn("late", self._names("user-two"))
        self.assertTrue(first.exists())

    def test_refresh_idle_logs_one_line_per_server_and_ignores_missing_credentials(self) -> None:
        folder = self._connect("user-one")
        self.manager.list_tools("user-one", "fake")
        (folder / "tokens.json").unlink()  # credentials removed behind the manager's back

        with self.assertLogs("recall.mcp", "INFO") as logs:
            self.assertEqual(self.manager.refresh_idle(), 0)
        self.assertEqual(len(logs.records), 1)

        self._connect("user-one")
        with self.assertLogs("recall.mcp", "INFO") as logs:
            self.assertEqual(self.manager.refresh_idle(), 1)
        self.assertEqual(len([r for r in logs.output if "refreshed MCP server fake for user user-one" in r]), 1)


class McpRefreshLoopTests(unittest.TestCase):
    def _service(self) -> ChatService:
        service = ChatService.__new__(ChatService)
        service._mcp = Mock()
        service._mcp_refresh_stop = threading.Event()
        return service

    def test_loop_waits_one_interval_then_refreshes_until_stopped(self) -> None:
        service = self._service()
        called = threading.Event()
        service._mcp.refresh_idle.side_effect = lambda: called.set()
        thread = threading.Thread(target=service._mcp_refresh_loop, args=(0.05,), daemon=True)
        start = time.monotonic()
        thread.start()
        self.assertTrue(called.wait(5))
        self.assertGreaterEqual(time.monotonic() - start, 0.04)
        service._mcp_refresh_stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_loop_survives_a_failing_round_and_does_not_run_before_the_interval(self) -> None:
        service = self._service()
        service._mcp.refresh_idle.side_effect = RuntimeError("boom")
        thread = threading.Thread(target=service._mcp_refresh_loop, args=(3600,), daemon=True)
        thread.start()
        time.sleep(0.1)
        service._mcp.refresh_idle.assert_not_called()
        service._mcp_refresh_stop.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())

        service = self._service()
        rounds = threading.Semaphore(0)

        def failing() -> None:
            rounds.release()
            raise RuntimeError("boom")

        service._mcp.refresh_idle.side_effect = failing
        thread = threading.Thread(target=service._mcp_refresh_loop, args=(0.02,), daemon=True)
        with self.assertLogs("recall.service", "WARNING"):
            thread.start()
            self.assertTrue(rounds.acquire(timeout=5))
            self.assertTrue(rounds.acquire(timeout=5))  # still looping after the error
            service._mcp_refresh_stop.set()
            thread.join(5)


class McpRefreshWebTests(unittest.TestCase):
    """POST /api/mcp/{id}/refresh, on a service with a real manager and the fake server."""

    _web_setup = UserPreferencesWebTests.setUp

    def setUp(self) -> None:
        self._web_setup()
        env = patch.dict(os.environ, {"RECALL_TEST_SHARED": "shared"})
        env.start()
        self.addCleanup(env.stop)
        self.manager = McpManager(_settings(), self.root)
        self.addCleanup(self.manager.close)
        self.service._mcp = self.manager

    def _connect(self, user_id: str, *flags: str) -> Path:
        folder = self.root / user_id / "mcp" / "fake"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "tokens.json").write_text("{}", encoding="utf-8")
        for flag in flags:
            (folder / flag).write_text("", encoding="utf-8")
        return folder

    def test_requires_login(self) -> None:
        self.client.post("/api/logout")
        self.assertEqual(self.client.post("/api/mcp/fake/refresh").status_code, 401)

    def test_unknown_server_is_404_and_not_connected_is_409(self) -> None:
        self.assertEqual(self.client.post("/api/mcp/missing/refresh").status_code, 404)
        response = self.client.post("/api/mcp/fake/refresh")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "mcp_not_connected"))

    def test_refresh_returns_status_and_count_and_tools_endpoint_is_fresh(self) -> None:
        folder = self._connect("user-one")
        before = self.client.get("/api/mcp/fake/tools").json()
        (folder / "late.flag").write_text("", encoding="utf-8")
        self.assertEqual(len(self.client.get("/api/mcp/fake/tools").json()), len(before))

        response = self.client.post("/api/mcp/fake/refresh")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["id"], body["connected"], body["tool_count"]), ("fake", True, len(before) + 1))
        names = [tool["name"] for tool in self.client.get("/api/mcp/fake/tools").json()]
        self.assertIn("late", names)

    def test_busy_connection_is_409(self) -> None:
        self._connect("user-one")
        self.client.get("/api/mcp/fake/tools")
        connection = self.manager._connections[("user-one", "fake")]
        connection.claim()
        try:
            response = self.client.post("/api/mcp/fake/refresh")
        finally:
            connection.release()
        self.assertEqual(response.status_code, 409)
