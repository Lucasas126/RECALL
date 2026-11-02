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


    def test_associative_settings_round_trip_through_the_store(self):
        chat_config = {"associative_memory": "question_only", "associative_execution": "sequential"}
        config = AgentConfig(id="aria", name="Aria", personality_prompt="Hi", chat_config=chat_config)
        self.store.create("user-a", config)
        loaded = self.store.load("user-a", "aria").chat_config
        self.assertEqual((loaded.associative_memory, loaded.associative_execution), ("question_only", "sequential"))

    def test_associative_defaults_and_legacy_boolean_in_agent_files(self):
        default = AgentConfig(id="aria", name="Aria", personality_prompt="Hi").chat_config
        self.assertEqual((default.associative_memory, default.associative_execution), ("llm", "parallel"))
        for legacy, mode in ((True, "llm"), (False, "off")):
            config = AgentConfig(
                id="aria", name="Aria", personality_prompt="Hi", chat_config={"associative_memory": legacy},
            )
            self.assertEqual(config.chat_config.associative_memory, mode)

    def test_unknown_associative_values_are_rejected(self):
        for field, value in (("associative_memory", "sometimes"), ("associative_execution", "later")):
            with self.assertRaises(ValidationError):
                AgentConfig(id="aria", name="Aria", personality_prompt="Hi", chat_config={field: value})


if __name__ == "__main__":
    unittest.main()
