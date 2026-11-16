from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import test_providers as providers_tests
import test_user_preferences as preferences_tests
from auth_helpers import login
from src.service.chat import AgentBusy, NoAgents, UnknownAgent
from src.service.providers import unavailable
from src.utils.i18n import InputError, UserMessageError, error_code


def _sse_events(response) -> list[tuple[str, object]]:
    events = []
    for frame in response.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


class ErrorCodeTests(unittest.TestCase):
    def test_codes_are_optional_stable_strings(self) -> None:
        self.assertIsNone(error_code(InputError("message is empty")))
        self.assertIsNone(error_code(ValueError("plain")))
        self.assertEqual(error_code(InputError("message is empty", code="x")), "x")
        self.assertEqual(error_code(AgentBusy("busy")), "agent_busy")
        self.assertEqual(error_code(NoAgents("none")), "no_agents")
        self.assertEqual(error_code(UnknownAgent("who")), "unknown_agent")
        self.assertIsInstance(NoAgents("none"), UserMessageError)

    def test_a_code_does_not_become_a_template_value(self) -> None:
        error = InputError("{name} is bad", code="c", name="n")
        self.assertEqual((str(error), error.code, error.values), ("n is bad", "c", {"name": "n"}))

    def test_unavailable_provider_reasons_have_their_own_codes(self) -> None:
        expected = {
            "no_api_key": "missing_api_key", "unreachable": "provider_unreachable",
            "cli_missing": "claude_not_installed", "logged_out": "claude_not_logged_in",
            "unknown": "provider_unavailable", "no_own_login": "claude_no_own_login",
        }
        for reason, code in expected.items():
            with self.subTest(reason=reason):
                row = {"id": "claude_subscription" if "claude" in code or reason == "unknown" else "openrouter",
                       "reason": reason, "detail": "VAR"}
                self.assertEqual(unavailable(row).code, code)


class NoAgentsWebTests(unittest.TestCase):
    setUp = preferences_tests.UserPreferencesWebTests.setUp

    def test_user_without_agents_gets_a_coded_error_with_a_next_step(self) -> None:
        self.client.post("/api/logout")
        login(self.client, "no-agents")
        response = self.client.get("/api/agents/agent-a/chats")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "no_agents")
        self.assertIn("Create one in the Agents tab", response.json()["detail"])
        self.assertEqual(self.client.get("/api/preferences").json()["agent_id"], None)

    def test_other_agent_errors_have_their_code_and_plain_errors_have_none(self) -> None:
        foreign = self.client.get("/api/agents/agent-c/chats")
        self.assertEqual((foreign.status_code, foreign.json()["code"]), (404, "unknown_agent"))
        invalid = self.client.patch("/api/preferences", json={"language": "fr"})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn("code", invalid.json())

    def _stream(self, error: Exception) -> list[tuple[str, object]]:
        def failing(*args, **kwargs):
            raise error
            yield  # pragma: no cover - makes this a generator, so the failure is reported in-band

        with patch.object(self.service, "send", side_effect=failing), \
                patch.object(self.service, "dream_command", return_value=None):
            response = self.client.post("/api/agents/agent-a/chats/c1/messages", json={"content": "hi"})
        self.assertEqual(response.status_code, 200)
        return _sse_events(response)

    def test_sse_error_event_carries_the_code_and_the_localized_message(self) -> None:
        events = self._stream(AgentBusy("wait for this chat's active and queued replies before changing models"))
        self.assertEqual([name for name, _ in events], ["error", "done"])
        payload = events[0][1]
        self.assertEqual(payload["code"], "agent_busy")
        self.assertIn("Espera a que terminen", payload["message"])  # user-one saves language es

    def test_sse_error_event_without_a_code_stays_a_plain_string(self) -> None:
        events = self._stream(RuntimeError("boom"))
        self.assertEqual(events[0], ("error", "boom"))


class ProviderErrorWebTests(unittest.TestCase):
    setUp = providers_tests.ProvidersApiTests.setUp

    def test_claude_logged_out_and_ollama_down_report_codes(self) -> None:
        response = self.client.get("/api/models/catalog", params={"provider": "claude_subscription"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "claude_not_logged_in")
        self.assertIn("claude auth login", response.json()["detail"])
        self.mocks["ollama"].side_effect = providers_tests.OllamaUnavailable("down")
        self.service._availability.forget()
        response = self.client.get("/api/models/catalog", params={"provider": "ollama"})
        self.assertEqual(response.json()["code"], "provider_unreachable")


if __name__ == "__main__":
    unittest.main()


class CompactStreamTests(unittest.TestCase):
    """`/compact` on the web says it is working first and gives the outcome as text when it ends."""
    setUp = preferences_tests.UserPreferencesWebTests.setUp

    def _post(self, events):
        with patch.object(self.service, "compact_start", return_value=events):
            return self.client.post("/api/agents/agent-a/chats/c1/messages", json={"content": "/compact"})

    def test_working_comes_before_the_text_and_the_stream_closes(self) -> None:
        from src.service.chat import ChatEvent
        response = self._post(iter([ChatEvent(kind="text", text="Context compacted: 9 → 3 tokens.")]))
        names = [name for name, _ in _sse_events(response)]
        self.assertEqual(names, ["working", "text", "done"])
        self.assertEqual(_sse_events(response)[1][1], "Context compacted: 9 → 3 tokens.")

    def test_a_failure_while_compacting_is_reported_in_band(self) -> None:
        def failing():
            raise RuntimeError("summary model down")
            yield
        names = [name for name, _ in _sse_events(self._post(failing()))]
        self.assertEqual(names, ["working", "error", "done"])
