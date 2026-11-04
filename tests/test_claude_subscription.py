from __future__ import annotations

import ctypes
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.config.models import (
    AssociativeMemoryConfig, ChatNamingConfig, ClaudeSubscriptionConfig, DreamConfig, ModelsConfig, ProviderConfig,
    ResolvedAgent, UserConfig,
)
from src.core.sources.conversations import ChatModel
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.claude_subscription import (
    ClaudeCodeLoggedOut, ClaudeCodeMissing, ClaudeCodeTooOld, ClaudeSubscriptionError,
    ClaudeSubscriptionProvider, claude_subscription_provider,
)
from src.providers.claude_subscription.catalog import native_model
from src.providers.catalog import OpenRouterModel
from src.service.chat import ChatService
from src.web.app import create_app
from auth_helpers import TEST_PASSWORD_HASH, login
from provider_helpers import CLAUDE_LOGGED_IN, CLAUDE_LOGGED_OUT, isolate_providers

FAKE = str(Path(__file__).with_name("fake_claude.py"))
TOOLS = [{"type": "function", "function": {
    "name": "get_time", "description": "Current time", "parameters": {"type": "object", "properties": {"zone": {"type": "string"}}},
}}]
# variables the env guard rejects; cleared so the developer's own environment cannot affect the tests
_GUARDED = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


def _provider(scenario: str, **kwargs) -> ClaudeSubscriptionProvider:
    return ClaudeSubscriptionProvider(command=[sys.executable, FAKE, "--scenario", scenario], timeout_seconds=20, **kwargs)


def _messages(text: str = "Hi") -> list[dict]:
    return [{"role": "system", "content": "You are a test."}, {"role": "user", "content": text}]


def _alive(pid: int) -> bool:
    if os.name == "nt":
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Env(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.record = Path(self.tmp.name) / "record.json"
        env = {key: value for key, value in os.environ.items() if key not in _GUARDED}
        env["FAKE_CLAUDE_RECORD"] = str(self.record)
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def recorded(self) -> dict:
        return json.loads(self.record.read_text(encoding="utf-8"))


class ClaudeSubscriptionProviderTests(_Env):
    def test_plain_reply_without_streaming(self) -> None:
        result = _provider("reply").invoke(_messages(), "claude_subscription:sonnet", stream=False)
        self.assertEqual(result["response"], "Hello world")
        self.assertIsNone(result["tool_calls"])
        self.assertEqual(result["usage"]["prompt_tokens"], 119)
        self.assertEqual(result["usage"]["completion_tokens"], 5)
        self.assertEqual(result["usage"]["prompt_tokens_details"], {"cached_tokens": 100, "cache_write_tokens": 7})
        argv = self.recorded()["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5-5[1m]")
        for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "1")
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_streaming_yields_content_then_final_dict(self) -> None:
        events = list(_provider("thinking").invoke(_messages(), "sonnet", reasoning=True, reasoning_effort="high"))
        self.assertEqual([e for e in events if isinstance(e, tuple)], [
            ("reasoning", "Let me"), ("reasoning", " think"), ("content", "Do"), ("content", "ne"),
        ])
        final = events[-1]
        self.assertEqual((final["response"], final["reasoning"]), ("Done", "Let me think"))
        argv = self.recorded()["argv"]
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_tool_call_round_trip_replays_signed_thinking(self) -> None:
        provider = _provider("tool")
        messages = _messages("What time is it?")
        first = provider.invoke(messages, "haiku", stream=False, tools=TOOLS)
        self.assertEqual(first["tool_calls"], [{"id": "toolu_1", "name": "get_time", "arguments": '{"zone": "UTC"}'}])
        manifest_frames = self.recorded()["frames"]
        self.assertEqual(len(manifest_frames), 1)
        messages += [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "get_time", "arguments": first["tool_calls"][0]["arguments"]}}]},
            {"role": "tool", "tool_call_id": "toolu_1", "name": "get_time", "content": "12:00"},
        ]
        second = provider.invoke(messages, "haiku", stream=False, tools=TOOLS)
        self.assertEqual(second["response"], "The tool said 12:00")
        frames = self.recorded()["frames"]
        self.assertEqual([f["type"] for f in frames], ["user", "assistant", "user"])
        self.assertIs(frames[0]["shouldQuery"], False)
        self.assertNotIn("shouldQuery", frames[-1])
        assistant = frames[1]["message"]["content"]
        self.assertEqual(assistant[0], {"type": "thinking", "thinking": "I need the time", "signature": "sig-1"})
        self.assertEqual(assistant[1]["name"], "mcp__recall__get_time")
        self.assertEqual(frames[2]["message"]["content"], [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "12:00"}])

    def test_cancellation_kills_the_process_tree(self) -> None:
        pidfile = Path(self.tmp.name) / "pids.json"
        os.environ["FAKE_CLAUDE_PIDFILE"] = str(pidfile)
        token = CancellationToken()
        events = _provider("hang").invoke(_messages(), "sonnet", cancellation=token)
        self.assertEqual(next(events), ("content", "par"))
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = json.loads(pidfile.read_text(encoding="utf-8"))
        self.assertTrue(all(_alive(pid) for pid in pids))
        threading.Timer(0.3, token.cancel).start()
        started = time.monotonic()
        with self.assertRaises(GenerationCancelled):
            for _ in events:
                pass
        self.assertLess(time.monotonic() - started, 10)
        deadline = time.monotonic() + 10
        while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse([pid for pid in pids if _alive(pid)])

    def test_native_errors_are_actionable(self) -> None:
        with self.assertRaisesRegex(ClaudeSubscriptionError, "plan limit reached: You've hit your session limit"):
            _provider("error").invoke(_messages(), "sonnet", stream=False)
        with self.assertRaisesRegex(ClaudeCodeLoggedOut, "claude auth login"):
            _provider("logged_out").invoke(_messages(), "sonnet", stream=False)

    def test_missing_cli_names_the_install_command(self) -> None:
        provider = ClaudeSubscriptionProvider(command=["definitely-not-an-installed-claude"])
        with self.assertRaisesRegex(ClaudeCodeMissing, "npm install -g @anthropic-ai/claude-code"):
            provider.invoke(_messages(), "sonnet", stream=False)

    def test_old_cli_asks_for_an_update(self) -> None:
        os.environ["FAKE_CLAUDE_VERSION"] = "2.1.200 (Claude Code)"
        with self.assertRaisesRegex(ClaudeCodeTooOld, r"needs 2\.1\.293 or newer\. Run `claude update`"):
            _provider("reply").invoke(_messages(), "haiku", stream=False)

    def test_env_guard_rejects_inherited_api_key_and_backend_overrides(self) -> None:
        os.environ["ANTHROPIC_API_KEY"] = "secret-value"
        os.environ["CLAUDE_CODE_USE_BEDROCK"] = "1"
        with self.assertRaises(ClaudeSubscriptionError) as caught:
            _provider("reply").invoke(_messages(), "sonnet", stream=False)
        self.assertIn("ANTHROPIC_API_KEY, CLAUDE_CODE_USE_BEDROCK", str(caught.exception))
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertFalse(self.record.exists())

    def test_telemetry_flags_identity_and_config_dir(self) -> None:
        os.environ.pop("USER", None)
        _provider("reply", config_dir="~/claude-login").invoke(_messages(), "sonnet", stream=False)
        env = self.recorded()["env"]
        for key in ("DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER"):
            self.assertEqual(env[key], "1")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], os.path.expanduser("~/claude-login"))
        self.assertTrue(env["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:"))
        self.assertEqual(env["CLAUDE_CODE_MAX_RETRIES"], "0")
        self.assertTrue(env["USER"] or env["USERNAME"])
        _provider("reply", telemetry=True).invoke(_messages(), "sonnet", stream=False)
        env = self.recorded()["env"]
        self.assertIsNone(env["DISABLE_TELEMETRY"])
        self.assertEqual(env["DISABLE_AUTOUPDATER"], "1")

    def test_mid_history_system_messages_become_user_text(self) -> None:
        messages = [
            {"role": "system", "content": "Soul"}, {"role": "system", "content": "[CUTOFF CONVERSATION]"},
            {"role": "user", "content": "Hi"}, {"role": "assistant", "content": "Partial"},
            {"role": "system", "content": "[associative memory] a hint"},
        ]
        _provider("reply").invoke(messages, "sonnet", stream=False)
        frames = self.recorded()["frames"]
        self.assertEqual(frames[-1]["message"]["content"], [{"type": "text", "text": "[System] [associative memory] a hint"}])
        argv = self.recorded()["argv"]
        system = Path(argv[argv.index("--system-prompt-file") + 1])
        self.assertFalse(system.exists())  # per-request files are removed afterwards

    def test_embeddings_are_refused(self) -> None:
        with self.assertRaisesRegex(ClaudeSubscriptionError, "embeddings"):
            ClaudeSubscriptionProvider().invoke_embeddings(["x"], "sonnet")

    def test_usage_note_marks_plan_usage(self) -> None:
        from src.agent.usage import subscription_note
        note = subscription_note({"provider": "claude_subscription", "list_price_estimate_usd": 0.0012,
                                  "admission": {"upstream_requests": 1, "blocked_requests": 1}})
        self.assertIn("Claude subscription (experimental)", note)
        self.assertIn("$0.0012 (not a charge)", note)
        self.assertIn("blocked extra requests=1", note)
        self.assertEqual(subscription_note({"prompt_tokens": 1}), "")

    def test_catalog_routes(self) -> None:
        self.assertEqual(native_model("claude_subscription:opus"), "claude-opus-5-5[1m]")
        self.assertEqual(native_model("claude-haiku-4-5"), "claude-haiku-4-5")
        self.assertEqual(ClaudeSubscriptionProvider().context_window("haiku"), 1_000_000)


class _Upstream(BaseHTTPRequestHandler):
    calls: list = []

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers["Content-Length"]))
        type(self).calls.append(self.headers.get("Authorization"))
        events = [
            {"type": "message_start", "message": {"id": "msg_up", "role": "assistant", "content": [], "usage": {"input_tokens": 20, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Relayed answer"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
            {"type": "message_stop"},
        ]
        body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002
        pass


class AdmissionRelayTests(_Env):
    def test_only_the_first_messages_request_reaches_the_upstream(self) -> None:
        _Upstream.calls = []
        server = HTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        provider = _provider("relay", upstream_url=f"http://127.0.0.1:{server.server_port}")
        result = provider.invoke(_messages(), "sonnet", stream=False)
        self.assertEqual(_Upstream.calls, ["Bearer fake"])
        self.assertEqual(result["response"], "Relayed answer")
        self.assertEqual(result["usage"]["admission"], {"upstream_requests": 1, "blocked_requests": 1})
        self.assertEqual((result["usage"]["prompt_tokens"], result["usage"]["completion_tokens"]), (20, 3))


class ClaudeSubscriptionConfigTests(unittest.TestCase):
    def test_provider_takes_the_advanced_options_and_has_no_enabled_switch(self) -> None:
        with self.assertRaises(ValidationError):
            ClaudeSubscriptionConfig(enabled=True)
        provider = claude_subscription_provider(ClaudeSubscriptionConfig(claude_config_dir="~/c", command="/opt/claude"))
        self.assertEqual((provider.config_dir, provider.command, provider.telemetry), ("~/c", ["/opt/claude"], False))

    def test_user_chat_model_accepts_references_but_not_for_embeddings_or_stt(self) -> None:
        self.assertEqual(ModelsConfig(chat="claude_subscription:sonnet").chat, "claude_subscription:sonnet")
        with self.assertRaisesRegex(ValidationError, "needs a model name"):
            ModelsConfig(chat="claude_subscription:")
        with self.assertRaisesRegex(ValidationError, "models.embeddings cannot use claude_subscription"):
            ModelsConfig(embeddings="claude_subscription:sonnet")
        with self.assertRaisesRegex(ValidationError, "models.stt cannot use claude_subscription"):
            ModelsConfig(stt="claude_subscription:haiku")
        with self.assertRaisesRegex(ValidationError, "models.stt cannot use ollama"):
            ModelsConfig(stt="ollama:whisper")
        self.assertEqual(ModelsConfig(embeddings="ollama:nomic-embed-text").embeddings, "ollama:nomic-embed-text")

    def test_claude_subscription_is_not_a_user_or_embeddings_provider(self) -> None:
        with self.assertRaisesRegex(ValidationError, "cannot be the main provider"):
            ProviderConfig(name="claude_subscription")
        with self.assertRaisesRegex(ValidationError, "unknown provider"):
            ProviderConfig(name="other")
        with self.assertRaisesRegex(ValidationError, "name of an environment variable"):
            ProviderConfig(name="openrouter", api_key="sk-or-v1-abc def")
        with self.assertRaisesRegex(ValidationError, "cannot be the main provider"):
            UserConfig(id="u", name="U", provider={"name": "openrouter"}, embeddings_provider={"name": "claude_subscription"}, agents=[])

    def test_framework_models_need_no_zdr_setting_unless_zdr_is_forced_on(self) -> None:
        self.assertEqual(ChatNamingConfig(model="claude_subscription:haiku").model, "claude_subscription:haiku")
        self.assertIsNone(AssociativeMemoryConfig(query_model="claude_subscription:haiku").zdr)
        self.assertEqual(ChatNamingConfig(model="claude_subscription:haiku", zdr=False).zdr, False)
        with self.assertRaisesRegex(ValidationError, "set chat_naming.zdr: false"):
            ChatNamingConfig(model="claude_subscription:haiku", zdr=True)
        with self.assertRaisesRegex(ValidationError, "set associative_memory.zdr: false"):
            AssociativeMemoryConfig(query_model="claude_subscription:haiku", zdr=True)

    def test_a_required_user_policy_rejects_claude_where_it_is_inherited(self) -> None:
        from src.agent import associative
        agent = Mock()
        query = AssociativeMemoryConfig(query_model="claude_subscription:haiku")
        with patch.object(associative, "ASSOC", query):
            agent.config.require_zdr = True
            with self.assertRaisesRegex(ValueError, "set associative_memory.zdr: false"):
                associative._query_model(agent)
            agent.config.require_zdr = False
            self.assertEqual(associative._query_model(agent)[2], False)
        with patch.object(associative, "ASSOC", AssociativeMemoryConfig(query_model="claude_subscription:haiku", zdr=False)):
            agent.config.require_zdr = True
            self.assertEqual(associative._query_model(agent)[2], False)

    def test_loading_a_zdr_user_refuses_a_claude_helper_model_that_inherits_the_policy(self) -> None:
        import src.config.loader as loader
        data = {"id": "u", "name": "U", "provider": {"name": "openrouter"}, "agents": ["a"]}
        naming = ChatNamingConfig(model="claude_subscription:haiku")
        for require_zdr, naming_config, fails in ((True, naming, True), (False, naming, False),
                                                  (True, ChatNamingConfig(model="claude_subscription:haiku", zdr=False), False),
                                                  (True, ChatNamingConfig(), False)):
            with self.subTest(require_zdr=require_zdr, zdr=naming_config.zdr),                     patch.object(loader, "_load_yaml", return_value={**data, "require_zdr": require_zdr}),                     patch.object(loader, "CHAT_NAMING", naming_config):
                if fails:
                    with self.assertRaisesRegex(ValueError, "set chat_naming.zdr: false"):
                        loader.load_user("u")
                else:
                    self.assertEqual(loader.load_user("u").require_zdr, require_zdr)

    def test_user_policy_defaults_to_off(self) -> None:
        self.assertFalse(UserConfig(id="u", name="U", provider={"name": "openrouter"}, agents=[]).require_zdr)
        self.assertTrue(UserConfig(id="u", name="U", provider={"name": "openrouter"}, agents=[], require_zdr=True).require_zdr)
        with self.assertRaises(ValidationError):
            UserConfig(id="u", name="U", provider={"name": "openrouter"}, agents=[], require_zdr="yes")

    def test_dream_model_and_chat_selection(self) -> None:
        self.assertEqual(DreamConfig(model="claude_subscription:opus").model, "claude_subscription:opus")
        selection = ChatModel(provider="claude_subscription", id="claude_subscription:sonnet", zdr=False)
        self.assertEqual(selection.provider, "claude_subscription")
        with self.assertRaises(ValidationError):
            ChatModel(provider="openrouter", id="claude_subscription:sonnet", zdr=False)
        with self.assertRaises(ValidationError):
            ChatModel(provider="claude_subscription", id="claude_subscription:sonnet", zdr=True)


class ClaudeSubscriptionWiringTests(unittest.TestCase):
    def _config(self, **claude) -> ResolvedAgent:
        return ResolvedAgent(
            user_id="u", id="a", name="A", models=ModelsConfig(), provider_name="openrouter", api_key="k",
            embeddings_provider_name="openrouter", embeddings_api_key="k",
            claude_subscription=ClaudeSubscriptionConfig(**claude),
        )

    def test_model_provider_routes_references(self) -> None:
        from src.agent.agent import model_provider
        from src.providers.openrouter import OpenRouterProvider
        self.assertIsInstance(model_provider(self._config(), "vendor/model"), OpenRouterProvider)
        provider = model_provider(self._config(claude_config_dir="/x"), "claude_subscription:sonnet")
        self.assertIsInstance(provider, ClaudeSubscriptionProvider)
        self.assertEqual(provider.config_dir, "/x")

    def test_agent_switches_providers_by_model_reference(self) -> None:
        from types import SimpleNamespace
        from src.agent.agent import Agent
        base, claude, ollama = Mock(), Mock(), Mock()
        made = {"claude_subscription": claude, "ollama": ollama}
        agent = SimpleNamespace(provider=base, config=SimpleNamespace(provider_name="openrouter"),
                                _providers={"openrouter": base})
        agent.provider_named = lambda name, purpose="": Agent.provider_named(agent, name, purpose)
        with patch("src.agent.agent.named_provider", side_effect=lambda config, name, purpose: made[name]) as factory:
            Agent._route_provider(agent, "claude_subscription:sonnet")
            self.assertIs(agent.provider, claude)
            Agent._route_provider(agent, "ollama:llama3:8b")
            self.assertIs(agent.provider, ollama)
            Agent._route_provider(agent, "vendor/model")
            self.assertIs(agent.provider, base)
            Agent._route_provider(agent, "claude_subscription:opus")
        self.assertEqual([call.args[1] for call in factory.call_args_list], ["claude_subscription", "ollama"])

    def test_associative_query_model_uses_the_users_claude_login(self) -> None:
        from src.agent import associative
        agent = Mock()
        agent.claude_provider.return_value = "claude-provider"
        query = AssociativeMemoryConfig(query_model="claude_subscription:haiku")
        agent.config.require_zdr = False
        with patch.object(associative, "ASSOC", query):
            self.assertEqual(associative._query_model(agent), ("claude-provider", "claude_subscription:haiku", False))
        agent.claude_provider.assert_called_once_with("associative_memory.query_model")

    def test_chat_naming_uses_the_requesting_users_claude_login(self) -> None:
        import src.service.chat as chat
        service = ChatService(agent_factory=Mock(), dream=Mock())
        conversations = Mock()
        conversations.generated_title.return_value = None
        provider = Mock()
        provider.invoke.return_value = {"response": "A title"}
        user = UserConfig(id="u", name="U", provider={"name": "openrouter"}, agents=["a"],
                          claude_subscription={"claude_config_dir": "/login"})
        naming = ChatNamingConfig(model="claude_subscription:haiku", min_messages=1)
        with patch.object(service, "_conversations", return_value=conversations), \
                patch.object(service, "_load", return_value=[{"role": "user", "content": "hi"}]), \
                patch.object(service, "_user", return_value=user), patch.object(chat, "CHAT_NAMING", naming), \
                patch.object(chat, "claude_subscription_provider", return_value=provider) as factory:
            service._name_chat("u", "a", "c")
        factory.assert_called_once_with(user.claude_subscription, purpose="chat_naming.model")
        self.assertEqual(provider.invoke.call_args.args[1], "claude_subscription:haiku")
        conversations.set_generated_title.assert_called_once_with("c", "A title")

    def test_chat_naming_with_claude_is_refused_while_the_user_requires_zdr(self) -> None:
        import src.service.chat as chat
        service = ChatService(agent_factory=Mock(), dream=Mock())
        conversations = Mock()
        conversations.generated_title.return_value = None
        user = UserConfig(id="u", name="U", provider={"name": "openrouter"}, agents=["a"], require_zdr=True)
        naming = ChatNamingConfig(model="claude_subscription:haiku", min_messages=1)
        with patch.object(service, "_conversations", return_value=conversations), \
                patch.object(service, "_load", return_value=[{"role": "user", "content": "hi"}]), \
                patch.object(service, "_user", return_value=user), patch.object(chat, "CHAT_NAMING", naming), \
                patch.object(chat, "claude_subscription_provider") as factory, \
                self.assertLogs("recall.service", level="ERROR") as logs:
            service._name_chat("u", "a", "c")
        factory.assert_not_called()
        conversations.set_generated_title.assert_not_called()
        self.assertIn("cannot be used while zero data retention is required", "\n".join(logs.output))


class ClaudeSubscriptionCatalogWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = UserConfig(
            id="test-user", name="Test", provider=ProviderConfig(name="openrouter"),
            password_hash=TEST_PASSWORD_HASH, agents=[],
        )
        self.mocks = isolate_providers(self, claude=CLAUDE_LOGGED_IN)
        self.client = TestClient(create_app(ChatService(agent_factory=Mock())))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        user_patch = patch("src.service.chat.load_user", return_value=self.user)
        user_patch.start()
        self.addCleanup(user_patch.stop)
        catalog = patch("src.service.providers.list_openrouter_models", return_value=[
            OpenRouterModel(id="vendor/alpha", name="Alpha", description="Test", zdr_providers=["Private"]),
        ])
        catalog.start()
        self.addCleanup(catalog.stop)
        login(self.client, self.user.id)

    def test_logged_in_users_see_subscription_models_marked_experimental(self) -> None:
        models = self.client.get("/api/models").json()
        # the merged list (Telegram) holds the available providers, the main provider's models first
        self.assertEqual([m["id"] for m in models], [
            "vendor/alpha", "claude_subscription:haiku", "claude_subscription:sonnet", "claude_subscription:opus",
        ])
        self.assertEqual([m["provider"] for m in models], ["openrouter"] + ["claude_subscription"] * 3)
        self.assertTrue(models[1]["experimental"])
        self.assertFalse(models[1]["zdr_applies"])
        self.assertEqual(models[1]["zdr_providers"], [])
        self.assertEqual(  # the OpenRouter catalog is mocked; only the subscription rows are filtered here
            [m["id"] for m in self.client.get("/api/models?q=sonnet").json()], ["vendor/alpha", "claude_subscription:sonnet"],
        )
        # a prefixed query skips the other providers
        self.assertEqual([m["id"] for m in self.client.get("/api/models?q=claude_subscription:opus").json()],
                         ["claude_subscription:opus"])

    def test_logged_out_claude_is_not_offered(self) -> None:
        self.mocks["claude"].return_value = CLAUDE_LOGGED_OUT
        self.assertEqual([m["id"] for m in self.client.get("/api/models").json()], ["vendor/alpha"])


if __name__ == "__main__":
    unittest.main()
