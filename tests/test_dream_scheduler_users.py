import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.service import chat
from src.service.chat import ChatService

_USER_YAML = """id: {name}
name: {name}
max_level: admin
provider:
  name: openrouter
  api_key: {key}
agents: [one]
"""


class SchedulerUsersTests(unittest.TestCase):
    """The dream scheduler lists only users whose provider key is available."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def _user(self, name: str, key: str) -> None:
        folder = self.root / name
        (folder / "agents").mkdir(parents=True)
        (folder / "user.yaml").write_text(_USER_YAML.format(name=name, key=key), encoding="utf-8")
        (folder / "agents" / "one.yaml").write_text("id: one\nname: One\n", encoding="utf-8")

    def test_user_without_its_key_is_skipped_without_an_error(self) -> None:
        self._user("with_key", "SCHEDULER_TEST_KEY_SET")
        self._user("no_key", "SCHEDULER_TEST_KEY_UNSET")
        with patch.dict(os.environ, {"SCHEDULER_TEST_KEY_SET": "x"}), \
                patch.object(chat, "CONFIG_DIR", self.root), \
                patch("src.config.loader.CONFIG_DIR", self.root), \
                self.assertNoLogs("recall", level="ERROR"):
            os.environ.pop("SCHEDULER_TEST_KEY_UNSET", None)
            pairs = ChatService._all_agents(None)
        self.assertEqual(pairs, [("with_key", "one")])


if __name__ == "__main__":
    unittest.main()
