"""The Telegram router offers a person's shared agents as well as their own."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from src.config.models import OrchestratorConfig
from src.providers.decisions import Decision
from tests.web.test_shared_agents_web import SHARED, SharedAgentFixture


def _decision(choice: str) -> Decision:
    return Decision(choice=choice, confidence=0.9, probabilities={choice: 0.9}, latency_ms=1, cost=None)


class SharedRouterTests(SharedAgentFixture):
    def setUp(self) -> None:
        super().setUp()
        for target, value in (
            ("src.service.chat.ORCHESTRATOR", OrchestratorConfig(enabled=True, api_key="ROUTER_KEY")),
            ("src.service.chat.get_secret", lambda name: "key"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_member_with_one_own_agent_gets_routing_thanks_to_the_shared_one(self) -> None:
        self.assertTrue(self.service.orchestrator_active("user-one"))

    def test_the_router_sees_own_and_shared_agents_and_can_pick_the_shared_one(self) -> None:
        with patch("src.service.chat.decide_choice", return_value=_decision(SHARED)) as decide:
            decision = self.service.route_message("user-one", "plan the trip together")
        self.assertEqual(set(decide.call_args.kwargs["criteria"]), {"agent-one", SHARED})
        self.assertEqual(decision.agent_id, SHARED)

    def test_a_non_member_never_sees_the_shared_agent(self) -> None:
        self.assertFalse(self.service.orchestrator_active("user-three"))  # only agent-one of their own
        with patch("src.service.chat.decide_choice", return_value=_decision("agent-one")) as decide:
            self.service.route_message("user-three", "anything")
        decide.assert_not_called()


if __name__ == "__main__":
    unittest.main()
