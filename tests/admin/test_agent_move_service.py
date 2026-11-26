"""ChatService.share_agent / unshare_agent: busy checks, preferences, index rebuild."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from tests.support.admin_helpers import AdminFixture
from src.config.preferences import UserPreferences
from src.config.spaces import all_spaces, clear_spaces_cache
from src.service.agent_move import MoveError
from src.service.chat import AgentBusy


class ServiceMoveTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        clear_spaces_cache()
        for target in ("src.admin.spaces.CONFIG_DIR", "src.admin.spaces.DATA_DIR"):
            patcher = patch(target, self.config_dir if "CONFIG" in target else self.data_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.config_dir / "user-b" / "agents").mkdir(parents=True)
        (self.config_dir / "user-b" / "user.yaml").write_text(
            "id: user-b\nname: B\nmax_level: admin\nprovider: {name: ollama, api_key: B_KEY}\nagents: []\n", encoding="utf-8")
        self.service._preferences.update("user-a", UserPreferences(agent_id="bot", default_agent_id="bot"))
        self.reindexed = []
        patcher = patch.object(type(self.service), "reindex", lambda s, u, a, full=False: self.reindexed.append((u, a, full)) or {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_busy_refused_then_share_updates_preferences_and_reindexes(self) -> None:
        slot = self.service._slot("user-a", "bot")
        slot.active["chat"] = object()
        with self.assertRaises(AgentBusy):
            self.service.share_agent("user-a", "bot", ["user-b"])
        slot.active.clear()
        space_id = self.service.share_agent("user-a", "bot", ["user-b"])
        self.assertEqual(self.reindexed, [(space_id, "bot", True)])
        prefs = self.service._preferences.get("user-a")
        self.assertIsNone(prefs.agent_id)
        self.assertIsNone(prefs.default_agent_id)

    def test_running_task_and_process_count_as_busy(self) -> None:
        self.assertFalse(self.service._agent_in_use("user-a", "bot"))
        running = [{"status": "running", "parent": {"agent_id": "other"}, "child": {"agent_id": "bot"}}]
        with patch.object(self.service._tasks.store, "list", return_value=running):
            self.assertTrue(self.service._agent_in_use("user-a", "bot"))
            with self.assertRaises(MoveError):
                self.service.share_agent("user-a", "bot", ["user-b"])
        process = [{"state": "running", "agent": "bot"}]
        with patch("src.service.chat.process_registry") as registry:
            registry.return_value.list.return_value = process
            self.assertTrue(self.service._agent_in_use("user-a", "bot"))

    def test_unshare_reindexes_each_copy(self) -> None:
        space_id = self.service.share_agent("user-a", "bot", ["user-b"])
        self.reindexed.clear()
        plan = self.service.unshare_agent("user-b", space_id, "bot")
        self.assertEqual(sorted(u for u, _, _ in self.reindexed), ["user-a", "user-b"])
        self.assertEqual(plan, {"user-a": "bot", "user-b": "bot"})
        self.assertEqual(all_spaces(self.config_dir), [])


if __name__ == "__main__":
    unittest.main()
