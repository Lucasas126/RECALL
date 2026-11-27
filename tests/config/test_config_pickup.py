"""Another process edits the yaml: the running service must see it on the next message, never crash on bad edits."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support.admin_helpers import AdminFixture, FakeAgent

from src.config.agents import AgentConfigError, AgentStore
from src.config.loader import load_agent_config, load_user
from src.service.chat import ChatService

ROOT = Path(__file__).resolve().parents[2]
CLI = (
    "import sys; sys.path.insert(0, sys.argv[1]); from src.admin.users import main; raise SystemExit(main(sys.argv[2:]))"
)


class PickupFixture(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        self.built: list[str] = []

        def factory(user_id: str, agent_id: str, chat_id: str | None) -> FakeAgent:
            name = load_agent_config(user_id, agent_id).name
            self.built.append(name)
            return FakeAgent(name)

        self.service = ChatService(agent_factory=factory)
        self.chat = self.service.create_chat("user-a", "bot")["id"]
        self.addCleanup(self.finish_workers)

    def finish_workers(self) -> None:
        for slot in self.service._slots.values():
            for worker in list(slot.workers.values()):
                worker.join(3)

    def say(self, text: str = "hi") -> tuple[str, list[str]]:
        """(the reply text, the notice texts) of one message."""
        events = list(self.service.send("user-a", "bot", self.chat, text))
        for slot in self.service._slots.values():
            for worker in list(slot.workers.values()):
                worker.join(3)
        reply = "".join(e.text for e in events if e.kind == "text")
        return reply, [e.text for e in events if e.kind == "notice"]

    def other_process(self, *argv: str) -> subprocess.CompletedProcess:
        """The admin CLI in a REAL second interpreter; its cwd holds `.config` and `data` (the relative settings paths)."""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        return subprocess.run([sys.executable, "-c", CLI, str(ROOT), *argv], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=120)


class CrossProcessPickupTests(PickupFixture):
    def test_a_cli_edit_in_another_process_is_seen_on_the_next_message(self) -> None:
        self.assertEqual(self.say()[0], "reply from Bot")
        self.assertEqual(self.say()[0], "reply from Bot")
        self.assertEqual(self.built, ["Bot"], "an unchanged config keeps the cached runtime")
        result = self.other_process("agent", "set", "--user", "user-a", "--agent", "bot", "--set", "name=Renamed")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.service.list_agents("user-a")[0]["name"], "Renamed")
        reply, notices = self.say()
        self.assertEqual((reply, notices), ("reply from Renamed", []))
        self.assertEqual(self.built, ["Bot", "Renamed"])
        self.say()
        self.assertEqual(self.built, ["Bot", "Renamed"])

    def test_a_cli_rename_and_a_new_agent_are_visible(self) -> None:
        result = self.other_process("agent", "create", "--user", "user-a", "--id", "extra", "--name", "Extra")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual([a["id"] for a in self.service.list_agents("user-a")], ["bot", "extra"])
        check = self.other_process("agent", "check")
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)
        self.assertIn("OK: 1 user(s), 2 agent(s)", check.stdout)

    def test_a_secrets_file_change_rebuilds_the_runtime(self) -> None:
        self.say()
        result = subprocess.run(
            [sys.executable, "-c", CLI, str(ROOT), "set-secret", "USER_A_KEY_NAME", "--stdin"], cwd=self.root,
            input="value-from-another-process\n", capture_output=True, text=True, timeout=120,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "saved USER_A_KEY_NAME")
        self.assertNotIn("value-from-another-process", result.stdout + result.stderr)
        self.say()
        self.assertEqual(self.built, ["Bot", "Bot"])


class InvalidEditResilienceTests(PickupFixture):
    def test_invalid_agent_yaml_keeps_the_previous_config_with_a_clear_message(self) -> None:
        self.assertEqual(self.say()[0], "reply from Bot")
        self.write_agent("bot", "id: bot\nname: Broken\nchat_config: {stream: maybe}\n")
        with self.assertLogs("recall.service", level="ERROR") as logs:
            reply, notices = self.say()
        self.assertEqual(reply, "reply from Bot")  # the previous good runtime keeps serving
        self.assertEqual(len(notices), 1)
        self.assertIn("invalid", notices[0])
        self.assertIn("chat_config.stream", notices[0])
        self.assertIn("bot", "".join(logs.output))
        self.assertEqual(self.service.list_agents("user-a"), [{"id": "bot", "name": "Bot", "avatar": None, "hidden": False}])
        self.assertEqual(self.service.list_agent_configs("user-a")[0]["name"], "Bot")
        self.assertEqual(self.built, ["Bot"])
        # fixing the file is picked up again
        self.write_agent("bot", "id: bot\nname: Fixed\n")
        self.assertEqual(self.say(), ("reply from Fixed", []))

    def test_yaml_syntax_errors_and_user_yaml_errors_do_not_crash(self) -> None:
        self.say()
        self.write_agent("bot", "id: bot\nname: [unclosed\n")
        reply, notices = self.say()
        self.assertEqual(reply, "reply from Bot")
        self.assertIn("line", notices[0])
        self.write_agent("bot", "id: bot\nname: Bot\n")
        self.write_user("id: user-a\nname: User A\nmax_level: admin\nprovider: {name: nope}\nagents: [bot]\n")
        self.assertEqual(self.service.list_agents("user-a"), [{"id": "bot", "name": "Bot", "avatar": None, "hidden": False}])
        self.assertEqual(self.service.login("user-a")["name"], "User A")
        reply, notices = self.say()
        self.assertEqual(reply, "reply from Bot")
        self.assertEqual(len(notices), 1)

    def test_the_problem_is_logged_once_per_distinct_error(self) -> None:
        self.say()
        self.write_agent("bot", "id: bot\nname: ''\n")
        with self.assertLogs("recall.service", level="ERROR") as logs:
            for _ in range(3):
                self.say()
        self.assertEqual(len([line for line in logs.output if "invalid configuration" in line]), 1)

    def test_a_service_without_a_good_config_reports_the_error_instead_of_crashing(self) -> None:
        self.write_agent("bot", "id: bot\nname: [unclosed\n")
        fresh = ChatService(agent_factory=lambda *a, **k: None)
        with self.assertRaises(Exception) as caught:
            fresh.get_agent("user-a", "bot")
        self.assertNotIn("Traceback", str(caught.exception))
        self.assertEqual(fresh.list_agents("user-a"), [{"id": "bot", "name": "bot", "avatar": None, "hidden": False}])


if __name__ == "__main__":
    unittest.main()


class OneInvalidAgentFileTests(PickupFixture):
    """A fresh service (no cached good config) still lists the valid agents and loads the preferences."""

    def setUp(self) -> None:
        super().setUp()
        self.write_agent("bad", "id: bad\nname: Bad\nmax_steps: 30\n")
        self.write_user(self.read("user-a/user.yaml").replace("agents: [bot]", "agents: [bot, bad]"))
        self.fresh = ChatService(agent_factory=lambda *a, **k: None)

    def test_agent_configs_mark_the_invalid_one(self) -> None:
        with self.assertLogs("recall.service", level="ERROR"):
            rows = self.fresh.list_agent_configs("user-a")
        self.assertEqual([row["id"] for row in rows], ["bot", "bad"])
        self.assertEqual(rows[0]["name"], "Bot")
        self.assertEqual((rows[1]["name"], rows[1]["broken"]), ("bad", True))
        self.assertIn("max_steps", rows[1]["error"])

    def test_preferences_load_and_list_both_agents(self) -> None:
        with self.assertLogs("recall.service", level="ERROR"):
            agents = self.fresh.get_preferences("user-a")["defaults"]["agents"]
        self.assertEqual(agents, [{"id": "bot", "name": "Bot"}, {"id": "bad", "name": "bad"}])

    def test_list_agents_is_unchanged(self) -> None:
        with self.assertLogs("recall.service", level="ERROR"):
            agents = self.fresh.list_agents("user-a")
        self.assertEqual(agents, [{"id": "bot", "name": "Bot", "avatar": None, "hidden": False}, {"id": "bad", "name": "bad", "avatar": None, "hidden": False}])


class UserFolderIdTests(PickupFixture):
    def test_an_id_that_differs_from_the_folder_is_refused(self) -> None:
        self.write_user(self.read("user-a/user.yaml").replace("id: user-a", "id: user-b"))
        with self.assertRaises(AgentConfigError) as raised:
            load_user("user-a")
        self.assertIn("'user-a'", str(raised.exception))
        self.assertIn("'user-b'", str(raised.exception))
        with self.assertRaises(AgentConfigError):
            AgentStore(self.config_dir, self.data_dir).user("user-a")

    def test_a_matching_id_loads(self) -> None:
        self.assertEqual(load_user("user-a").id, "user-a")
        self.assertEqual(AgentStore(self.config_dir, self.data_dir).user("user-a").id, "user-a")
