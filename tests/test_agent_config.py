import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.config.agents import AgentStore
from src.config.models import AgentConfig


class AgentDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        user_dir = root / "config" / "user-a"
        user_dir.mkdir(parents=True)
        (user_dir / "user.yaml").write_text(
            "id: user-a\nname: User A\nprovider: {name: openrouter}\nagents: []\n", encoding="utf-8",
        )
        self.store = AgentStore(root / "config", root / "data")

    def tearDown(self):
        self.tmp.cleanup()

    def test_description_defaults_to_empty(self):
        self.assertEqual(AgentConfig(id="aria", name="Aria", personality_prompt="Hi").description, "")

    def test_description_is_limited_to_500_characters(self):
        AgentConfig(id="aria", name="Aria", description="x" * 500, personality_prompt="Hi")
        with self.assertRaises(ValidationError):
            AgentConfig(id="aria", name="Aria", description="x" * 501, personality_prompt="Hi")

    def test_description_round_trips_through_the_store(self):
        config = AgentConfig(id="aria", name="Aria", description="Plans my week", personality_prompt="Hi")
        self.store.create("user-a", config)
        self.assertEqual(self.store.load("user-a", "aria").description, "Plans my week")

        self.store.update("user-a", "aria", config.model_copy(update={"description": "Notes"}))
        self.assertEqual(self.store.list_agents("user-a")[0].description, "Notes")


if __name__ == "__main__":
    unittest.main()
