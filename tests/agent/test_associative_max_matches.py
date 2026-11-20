from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.agent import associative
from src.agent.agent import Agent
from src.config.models import AssociativeMemoryConfig, ModelsConfig


def _agent(cap: int | None) -> SimpleNamespace:
    hits = [{"score": 0.9 - i * 0.05, "address": f"a{i}", "document": f"doc {i}"} for i in range(10)]
    hits.append({"score": 0.1, "address": "low", "document": "below threshold"})
    return SimpleNamespace(
        config=SimpleNamespace(associative_memory="question_only"),
        associative_max_matches=cap,
        query_memory=MagicMock(return_value=hits),
    )


class AssociativeMaxMatchesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AssociativeMemoryConfig(top_n=3, score_threshold=0.5, query_model=None)
        self.enterContext(patch.object(associative, "ASSOC", self.config))

    def test_user_value_overrides_the_application_default(self) -> None:
        agent = _agent(6)
        _, count = associative.recall(agent, "question")
        self.assertEqual(count, 6)
        self.assertGreaterEqual(agent.query_memory.call_args.args[1], 6)  # more candidates than the cap

    def test_unset_follows_settings_top_n(self) -> None:
        agent = _agent(None)
        _, count = associative.recall(agent, "question")
        self.assertEqual(count, 3)
        self.config.top_n = 5
        self.assertEqual(associative.recall(agent, "question")[1], 5)

    def test_threshold_stays_application_wide(self) -> None:
        agent = _agent(20)
        text, count = associative.recall(agent, "question")
        self.assertEqual(count, 9)  # fragments under 0.5 (0.45, 0.1) fragment is dropped whatever the user's maximum
        self.assertNotIn("below threshold", text)

    def test_refresh_applies_a_changed_value_on_the_next_reply(self) -> None:
        agent = object.__new__(Agent)
        agent.sources = MagicMock()
        agent.sources.conversations().model_selection.return_value = None
        agent.chat_id = "chat"
        agent.config = SimpleNamespace(models=ModelsConfig(), require_zdr=False)
        agent._route_provider = MagicMock()
        agent._max_context = agent._ctx_input_tokens = agent._prev_breakdown = None
        models = ModelsConfig()
        with patch("src.agent.agent.chat_zdr", return_value=False):
            agent.refresh_user_models(models, 4)
            self.assertEqual(associative.max_matches(agent), 4)
            agent.refresh_user_models(models, None)
            self.assertEqual(associative.max_matches(agent), self.config.top_n)
            agent.refresh_user_models(models)
            self.assertIsNone(agent.associative_max_matches)


if __name__ == "__main__":
    unittest.main()
