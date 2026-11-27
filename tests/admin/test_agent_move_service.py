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

    def test_busy_refused_then_share_keeps_preferences_and_reindexes(self) -> None:
        slot = self.service._slot("user-a", "bot")
        slot.active["chat"] = object()
        with self.assertRaises(AgentBusy):
            self.service.share_agent("user-a", "bot", ["user-b"])
        slot.active.clear()
        space_id = self.service.share_agent("user-a", "bot", ["user-b"])
        self.assertEqual(self.reindexed, [(space_id, "bot", True)])
        prefs = self.service._preferences.get("user-a")  # still reachable through the space: nothing to drop
        self.assertEqual((prefs.agent_id, prefs.default_agent_id), ("bot", "bot"))

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


    def test_preferences_of_a_member_who_leaves_drop_the_agent(self) -> None:
        self.service.share_agent("user-a", "bot", ["user-b"])
        self.service.update_preferences("user-b", UserPreferences(agent_id="bot"))
        self.service.leave_shared("user-b", "bot")
        self.assertIsNone(self.service._preferences.get("user-b").agent_id)
        self.assertEqual(self.service._preferences.get("user-a").agent_id, "bot")

    def _orphan_sequence(self) -> None:
        """share, unshare (both get a private copy), user-a deletes hers: config gone, data folder kept."""
        space_id = self.service.share_agent("user-a", "bot", ["user-b"])
        self.service.unshare_agent("user-b", space_id, "bot")
        self.service.delete_agent("user-a", "bot")
        (self.data_dir / "user-a" / "bot" / "old.txt").write_text("old", encoding="utf-8")

    def test_configured_copy_still_blocks_sharing(self) -> None:
        space_id = self.service.share_agent("user-a", "bot", ["user-b"])
        self.service.unshare_agent("user-b", space_id, "bot")
        with self.assertRaises(MoveError):
            self.service.share_agent("user-b", "bot", ["user-a"])  # user-a still has a configured `bot`

    def test_orphan_data_does_not_block_unshare_after_regroup(self) -> None:
        self._orphan_sequence()
        space = self.service.share_agent("user-b", "bot", ["user-a"])  # accepted invitation
        space = self.service._regroup("user-b", space, "bot", ["user-b"])  # user-a leaves
        space = self.service._regroup("user-b", space, "bot", ["user-a", "user-b"])  # invited again
        space = self.service._regroup("user-b", space, "bot", ["user-a"])  # user-b leaves
        plan = self.service.unshare_agent("user-a", space, "bot")
        self.assertEqual(plan, {"user-a": "bot"})
        self.assertIn("bot", self.service._user("user-a").agents)
        trash = list((self.data_dir / "user-a" / ".trash").glob("bot-*"))
        self.assertEqual(len(trash), 1)  # the old folder was kept, not deleted
        self.assertEqual((trash[0] / "old.txt").read_text(encoding="utf-8"), "old")
        self.assertFalse((self.data_dir / "user-a" / "bot" / "old.txt").exists())

    def test_orphan_data_is_trashed_on_share_and_audited(self) -> None:
        self._orphan_sequence()
        self.service.share_agent("user-b", "bot", ["user-a"])
        self.assertEqual(len(list((self.data_dir / "user-a" / ".trash").glob("bot-*"))), 0)  # user-a's folder is not touched
        lines = (self.data_dir / "user-b" / "admin_audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("orphan_data_moved_to", lines)

    def test_orphan_data_keeps_the_free_id(self) -> None:
        self._orphan_sequence()
        space = self.service.share_agent("user-b", "bot", ["user-a"])
        plan = self.service.unshare_agent("user-a", space, "bot")
        self.assertEqual(plan, {"user-a": "bot", "user-b": "bot"})  # no "-2" because of the orphan folder
        self.assertTrue(any((self.data_dir / "user-a" / ".trash").glob("bot-*")))
        audit = (self.data_dir / "user-a" / "admin_audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("orphan_data_moved_to", audit)


    def test_copy_with_same_id_blocks_invite_and_accept_leaves_it_untouched(self) -> None:
        from src.service.chat import InvitationError

        space_id = self.service.share_agent("user-a", "bot", ["user-b"])
        self.service.unshare_agent("user-b", space_id, "bot")  # both keep a private `bot`
        (self.data_dir / "user-a" / "bot" / "chat.txt").write_text("mine", encoding="utf-8")
        with self.assertRaises(InvitationError):
            self.service.invite("user-b", "bot", "user-a")  # refused early
        self.assertEqual(self.service._invitation_store().all(), [])
        with self.assertRaises(MoveError):  # and the move itself still refuses
            self.service.share_agent("user-b", "bot", ["user-a"])
        self.assertEqual(self.service._user("user-a").agents, ["bot"])
        self.assertTrue((self.config_dir / "user-a" / "agents" / "bot.yaml").is_file())
        self.assertEqual((self.data_dir / "user-a" / "bot" / "chat.txt").read_text(encoding="utf-8"), "mine")
        self.assertEqual(self.service._user("user-b").agents, ["bot"])


if __name__ == "__main__":
    unittest.main()


class RoundThreeServiceTests(ServiceMoveTests):
    def test_deleting_a_private_copy_after_unshare_leaves_clean_preferences(self) -> None:
        space_id = self.service.share_agent("user-a", "bot", ["user-b"])
        self.service.update_preferences("user-b", UserPreferences(agent_id="bot", default_agent_id="bot"))
        self.service.unshare_agent("user-b", space_id, "bot")
        self.service.delete_agent("user-b", "bot")
        prefs = self.service.get_preferences("user-b")  # must not raise "not owned"
        self.assertIsNone(prefs["agent_id"])

    def test_only_member_cannot_leave_with_a_clear_message(self) -> None:
        from src.service.chat import InvitationError
        self.service.share_agent("user-a", "bot", ["user-b"])
        self.service.leave_shared("user-b", "bot")
        with self.assertRaises(InvitationError) as ctx:
            self.service.leave_shared("user-a", "bot")
        self.assertIn("Use Unshare", str(ctx.exception))
