"""Phase 5 of the subagents design: a task handed to an EXISTING agent of the same user."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from test_subagents import LIMITS, SubagentFixture, mark

from src.agent.agent import Agent
from src.agent.profile import AgentProfile
from src.agent.toolsets.delegate import TaskError, build_delegate_tools, delegate_notice
from src.config.models import (
    AgentConfig, AgentDelegationConfig, AgentTerminalBackend, AgentTerminalConfig, ChatConfig, DelegationTarget,
    ModelsConfig, ResolvedAgent,
)
from src.core.review import ReviewQueue
from src.core.sources.conversations import ConversationsSource
from src.core.sources.dream import DreamStateSource
from src.providers.cancellation import CancellationToken
from src.terminal.context import RunContext
from src.utils.i18n import InputError

TARGET = {"agent": "agent-two", "remember": False}
REMEMBER = {"agent": "agent-two", "remember": True}
WRITE_TOOLS = {"write", "append", "edit", "move", "delete"}


class AgentTargetFixture(SubagentFixture):
    """agent-one (the parent) may delegate to agent-two, which has its own SOUL and its own tool groups."""

    groups = ["delegate", "web"]
    target_groups = ["web", "memory_read", "memory_edit", "self"]
    target_terminal: AgentTerminalConfig | None = None

    def setUp(self) -> None:
        self.target_calls: list[tuple[str, str]] = []  # (system prompt, last user text) of every agent-two model call
        self.target_hold = False
        super().setUp()
        configs = patch("src.service.chat.load_agent_config", side_effect=self._agent_config_for)
        configs.start()
        self.addCleanup(configs.stop)

    def _agent_config_for(self, user: str, agent: str) -> AgentConfig:
        groups = self.target_groups if agent == "agent-two" else self.groups
        return AgentConfig(
            id=agent, name=f"Name of {agent}", chat_config=ChatConfig(tools=True, tool_groups=list(groups)),
            terminal=self.target_terminal if agent == "agent-two" else None,
        )

    def _factory(self, user: str, agent: str, chat_id: str | None) -> Agent:
        groups = self.target_groups if agent == "agent-two" else self.groups
        config = ResolvedAgent(
            user_id=user, id=agent, name=f"Name of {agent}", models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="", tools=True, tool_groups=list(groups),
            delegation=AgentDelegationConfig(targets=[DelegationTarget(**TARGET)]) if agent == "agent-one" else None,
            associative_memory=self.associative_memory,
        )
        instance = Agent(config, self.providers[(user, agent)], chat_id, profile=AgentProfile(soul=f"SOUL OF {agent}"))
        self.agents[(user, agent)] = instance
        return instance

    def _script(self, key):
        parent_script = super()._script(key)

        def respond(messages, model, *, cancellation: CancellationToken, **kwargs):
            if key[1] != "agent-two":
                yield from parent_script(messages, model, cancellation=cancellation, **kwargs)
                return
            user_text = next(m for m in reversed(messages) if m["role"] == "user")["content"]
            self.target_calls.append((messages[0]["content"], str(user_text)))
            gate = self.gates.get(re.sub(r"^\[[^\]]*\] ?", "", str(user_text)))
            if gate is not None:
                gate[0].set()
                gate[1].wait(5)
            yield "content", "AGENT-TWO DONE"
            if self.target_hold:
                while not self.release_child.is_set():
                    cancellation.raise_if_cancelled()
                    self.release_child.wait(0.02)
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return respond

    def to_agent(self, parent: RunContext | None = None, *, targets=None, **overrides):
        values = dict(agent="agent-two", targets=[TARGET] if targets is None else targets, parent_name="Parent Agent")
        return self.launch(parent, **{**values, **overrides})

    def finish(self, live) -> dict:
        self.assertTrue(live.done.wait(8))
        return self.record(live.task_id)

    def parent_of(self, agent: str) -> RunContext:
        chat = self.service.create_chat("user-one", agent)["id"]
        return RunContext("user-one", agent, chat, None, lambda *a: None, CancellationToken(), tasks=self.manager)


class AllowlistTests(AgentTargetFixture):
    def test_only_a_listed_agent_can_be_a_target(self) -> None:
        for targets in ([], [{"agent": "other", "remember": False}]):
            with self.subTest(targets=targets), self.assertRaises(TaskError) as caught:
                self.to_agent(targets=targets)
            self.assertIn("not one of your delegation targets", str(caught.exception))
        self.assertEqual(self.manager.store.list("user-one"), [])

    def test_an_agent_of_another_user_cannot_be_reached(self) -> None:
        # listed in the parent's config but not one of user-one's agents (it would belong to user-two): refused
        with self.assertRaises(TaskError) as caught:
            self.to_agent(agent="agent-of-user-two", targets=[{"agent": "agent-of-user-two", "remember": False}])
        self.assertIn("not available", str(caught.exception))
        self.assertEqual(self.manager.store.list("user-one"), [])

    def test_the_tool_has_no_user_parameter_and_names_the_allowed_agents(self) -> None:
        config = ResolvedAgent(
            user_id="user-one", id="agent-one", name="One", models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="", tool_groups=["delegate"],
            delegation=AgentDelegationConfig(targets=[DelegationTarget(agent="agent-two")]),
        )
        spec = build_delegate_tools(config)[0].tool_spec["function"]
        self.assertNotIn("user", spec["parameters"]["properties"])
        self.assertEqual(spec["parameters"]["properties"]["agent"]["enum"], ["", "agent-two"])
        self.assertIn("agent-two", spec["description"])
        self.assertIn("agent-two", delegate_notice(config))
        plain = build_delegate_tools(config.model_copy(update={"delegation": AgentDelegationConfig()}))[0].tool_spec["function"]
        self.assertNotIn("agent", plain["parameters"]["properties"])

    def test_the_tool_passes_the_agent_and_the_targets(self) -> None:
        config = ResolvedAgent(
            user_id="user-one", id="agent-one", name="One", models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="", tool_groups=["delegate"],
            delegation=AgentDelegationConfig(targets=[DelegationTarget(agent="agent-two", remember=True)]),
        )
        tasks = Mock()
        tasks.start.return_value = {"id": "x", "status": "running"}
        run = RunContext("user-one", "agent-one", self.chat, None, lambda *a: None, CancellationToken(), tasks=tasks)
        with patch("src.agent.toolsets.delegate.current_run", return_value=run):
            build_delegate_tools(config)[0](goal="g", agent="agent-two")
        values = tasks.start.call_args.kwargs
        self.assertEqual((values["agent"], values["parent_name"]), ("agent-two", "One"))
        self.assertEqual(values["targets"], [{"agent": "agent-two", "remember": True}])


class CycleAndDepthTests(AgentTargetFixture):
    def test_an_agent_cannot_delegate_to_itself(self) -> None:
        with self.assertRaises(TaskError) as caught:
            self.to_agent(agent="agent-one", targets=[{"agent": "agent-one", "remember": False}])
        self.assertIn("itself", str(caught.exception))

    def test_a_to_b_while_b_waits_on_a_is_refused(self) -> None:
        self.target_hold = True
        self.agent_one_runs = self.to_agent  # B (agent-two) runs a task launched by A below
        first = self.to_agent()  # agent-one -> agent-two, running
        self.assertEqual(self.record(first.task_id)["child"]["agent_id"], "agent-two")
        back = self.parent_of("agent-two")
        with self.assertRaises(TaskError) as caught:  # agent-two -> agent-one would close the loop
            self.to_agent(back, agent="agent-one", targets=[{"agent": "agent-one", "remember": False}])
        self.assertIn("cycle refused", str(caught.exception))
        self.release_child.set()
        self.finish(first)

    def test_the_other_direction_is_fine_once_nothing_waits(self) -> None:
        first = self.to_agent()
        self.finish(first)
        back = self.parent_of("agent-two")
        live = self.to_agent(back, agent="agent-one", targets=[{"agent": "agent-one", "remember": False}])
        self.assertEqual(self.finish(live)["status"], "done")

    def test_a_worker_chat_of_the_target_cannot_launch_depth_one(self) -> None:
        live = self.to_agent()
        record = self.finish(live)
        worker_chat = record["child"]["chat_id"]
        worker = RunContext("user-one", "agent-two", worker_chat, None, lambda *a: None, CancellationToken(), tasks=self.manager)
        with self.assertRaises(TaskError) as caught:
            self.to_agent(worker, agent="agent-one", targets=[{"agent": "agent-one", "remember": False}])
        self.assertIn("depth 1", str(caught.exception))
        self.assertNotIn("task_start", self.agents[("user-one", "agent-two")]._tool_handlers)


class RunAsTheTargetTests(AgentTargetFixture):
    def test_the_target_runs_with_its_own_soul_and_the_instructions_are_ignored(self) -> None:
        live = self.to_agent(instructions="You are a poet.", goal="summarise X", context="facts")
        record = self.finish(live)
        self.assertEqual(record["status"], "done")
        system, message = self.target_calls[0]
        self.assertIn("SOUL OF agent-two", system)
        self.assertNotIn("poet", system)
        self.assertNotIn("worker agent", system)
        self.assertRegex(message, r"^(\[[^\]]*\] )?Task from Parent Agent \(user-one\)")
        self.assertIn("Goal:\nsummarise X", message)
        self.assertIn("Context:\nfacts", message)
        self.assertEqual(record["result"]["text"], "AGENT-TWO DONE")
        self.assertEqual(record["target"], {"kind": "agent", "agent_id": "agent-two", "name": "Name of agent-two", "remember": False})

    def test_the_worker_chat_lives_under_the_target(self) -> None:
        record = self.finish(self.to_agent())
        chat = record["child"]["chat_id"]
        self.assertEqual(record["child"]["agent_id"], "agent-two")
        two, one = self.sources[("user-one", "agent-two")], self.sources[("user-one", "agent-one")]
        self.assertTrue(two.is_task_chat(chat))
        self.assertFalse(one.exists_chat(chat))
        self.assertNotIn(chat, two.chats())  # hidden from the lists
        self.assertEqual(two.task_mark(chat).parent_agent_id, "agent-one")
        self.assertEqual(two.task_mark(chat).kind, "agent")
        self.assertEqual(self.service.get_task_chat("user-one", record["id"])["task"]["agent_id"], "agent-two")

    def test_the_target_keeps_its_own_model_settings(self) -> None:
        self.sources[("user-one", "agent-one")].set_model(self.chat, "llama3", main="ollama")
        record = self.finish(self.to_agent(model="other-model"))
        self.assertIsNone(self.sources[("user-one", "agent-two")].model_selection(record["child"]["chat_id"]))

    def test_the_groups_are_the_grant_intersected_with_the_targets_own(self) -> None:
        self.target_groups = ["memory_read"]  # no web
        record = self.finish(self.to_agent(groups=["web"]))
        self.assertEqual(record["grant"]["groups"], [])
        self.assertEqual(self.agents[("user-one", "agent-two")]._tool_handlers.keys() & {"web_search", "web_retrieve"}, set())

    def test_a_granted_group_the_target_has_is_kept_and_the_rest_of_its_groups_are_not_added(self) -> None:
        record = self.finish(self.to_agent(groups=["web"]))
        handlers = self.agents[("user-one", "agent-two")]._tool_handlers
        self.assertEqual(record["grant"]["groups"], ["web"])
        self.assertTrue({"web_search", "web_retrieve"} <= handlers.keys())
        self.assertFalse(WRITE_TOOLS & handlers.keys())
        self.assertNotIn("task_start", handlers)

    def test_a_group_the_parent_cannot_grant_is_still_refused(self) -> None:
        with self.assertRaises(TaskError) as caught:
            self.to_agent(groups=["memory_edit"])
        self.assertIn("cannot grant memory_edit", str(caught.exception))

    def test_terminal_backends_need_the_target_to_have_them(self) -> None:
        with self.assertRaises(TaskError) as caught:  # agent-two has no terminal group
            self.to_agent(backends=["box"], grantable_backends=["box"])
        self.assertIn("no terminal", str(caught.exception))
        self.target_groups = ["web", "terminal"]
        self.target_terminal = AgentTerminalConfig(backends={"box": AgentTerminalBackend(approvals="smart")})
        with self.assertRaises(TaskError) as caught:
            self.to_agent(backends=["lab"], grantable_backends=["box", "lab"])
        self.assertIn("has no backend lab", str(caught.exception))
        # both have box: allowed, and the worker is on manual approvals whatever the target's own mode
        live = self.to_agent(backends=["box"], grantable_backends=["box"])
        self.assertIn("terminal", self.record(live.task_id)["grant"]["groups"])
        self.finish(live)
        mark_ = self.sources[("user-one", "agent-two")].task_mark(self.record(live.task_id)["child"]["chat_id"])
        self.assertEqual(mark_.backends, ["box"])

    def test_the_wake_goes_to_the_parent_chat_not_the_targets(self) -> None:
        events: list[dict] = []
        self.manager.subscribe(events.append)
        live = self.to_agent()
        self.finish(live)
        finished = next(e for e in events if e["kind"] == "task_finished")
        self.assertEqual((finished["agent_id"], finished["chat_id"]), ("agent-one", self.chat))

    def test_the_approval_attribution_shows_the_targets_name(self) -> None:
        live = self.to_agent()
        record = self.finish(live)
        context = RunContext("user-one", "agent-two", record["child"]["chat_id"], None, lambda *a: None, CancellationToken())
        mark_ = self.sources[("user-one", "agent-two")].task_mark(record["child"]["chat_id"])
        self.manager.bind_worker(context, mark_)
        self.assertEqual(context.task["worker"], "Name of agent-two")
        self.assertEqual(self.manager.view(record)["target"]["name"], "Name of agent-two")


class RememberTests(AgentTargetFixture):
    def _tools(self, remember: bool) -> tuple[set[str], str]:
        targets = [REMEMBER if remember else TARGET]
        record = self.finish(self.to_agent(targets=targets))
        return set(self.agents[("user-one", "agent-two")]._tool_handlers), record["child"]["chat_id"]

    def test_without_remember_memory_edits_are_blocked_and_the_chat_is_not_learned_from(self) -> None:
        tools, chat = self._tools(False)
        source = self.sources[("user-one", "agent-two")]
        self.assertFalse(tools & (WRITE_TOOLS | {"memory", "skill_manage", "soul_edit"}))
        self.assertTrue(source.is_disposable(chat))
        self.assertTrue(self.agents[("user-one", "agent-two")]._disposable)

    def test_with_remember_the_agent_keeps_its_own_memory_tools_and_nothing_else_about_itself(self) -> None:
        tools, chat = self._tools(True)
        source = self.sources[("user-one", "agent-two")]
        self.assertTrue(WRITE_TOOLS <= tools)
        self.assertIn("memory", tools)
        self.assertFalse(tools & {"soul_edit", "skill_manage", "task_start"})
        self.assertFalse(source.is_disposable(chat))
        self.assertTrue(source.is_task_chat(chat))  # hidden although ordinary
        self.assertNotIn(chat, source.chats())
        self.assertIn(chat, source.chats(include_internal=True))
        self.assertFalse(self.agents[("user-one", "agent-two")]._disposable)

    def test_a_remember_task_is_reviewable_by_dream_and_a_plain_one_is_not(self) -> None:
        with TemporaryDirectory() as directory:
            chats = ConversationsSource(Path(directory) / "chats")
            state = DreamStateSource(Path(directory) / "dream")
            remembered = chats.create(origin="task", task=mark("aaaaaaaaaaaa").model_copy(update={"kind": "agent", "remember": True}))
            plain = chats.create(origin="task", task=mark("bbbbbbbbbbbb").model_copy(update={"kind": "agent"}))
            for chat in (remembered, plain):
                chats.append(chat, "user", "q")
                chats.append(chat, "assistant", "a")
            pending = [item["chat_id"] for item in ReviewQueue(chats, state).pending()]
            self.assertEqual(pending, [remembered])

    def test_remember_is_refused_while_the_target_is_replying_elsewhere(self) -> None:
        started, release = self._gate("busy")
        self.service.send("user-one", "agent-two", self.service.create_chat("user-one", "agent-two")["id"], "busy")
        self.assertTrue(started.wait(3))
        try:
            with self.assertRaises(TaskError) as caught:
                self.to_agent(targets=[REMEMBER])
            self.assertIn("not safe in parallel", str(caught.exception))
            live = self.to_agent(targets=[TARGET])  # read-only targets may run next to another reply
            release.set()
            self.finish(live)
        finally:
            release.set()

    def test_a_second_remember_task_waits_for_the_first(self) -> None:
        self.target_hold = True
        first = self.to_agent(targets=[REMEMBER])
        with self.assertRaises(TaskError) as caught:
            self.to_agent(targets=[REMEMBER])
        self.assertRegex(str(caught.exception), "already runs a task that writes its memory|not safe in parallel")
        self.release_child.set()
        self.finish(first)


class DelegationConfigTests(unittest.TestCase):
    def test_targets_are_validated(self) -> None:
        with self.assertRaises(ValueError):
            AgentDelegationConfig(targets=[DelegationTarget(agent="b"), DelegationTarget(agent="b", remember=True)])
        with self.assertRaises(ValueError):
            DelegationTarget.model_validate({"agent": "b", "remember": "yes"})
        with self.assertRaises(ValueError):
            DelegationTarget.model_validate({"agent": "b", "user": "someone-else"})
        with self.assertRaises(ValueError):
            AgentConfig(id="a", name="A", chat_config=ChatConfig(tool_groups=["delegate"]),
                        delegation=AgentDelegationConfig(targets=[DelegationTarget(agent="a")]))

    def test_the_block_survives_a_dump_and_load(self) -> None:
        config = AgentConfig(
            id="a", name="A", chat_config=ChatConfig(tool_groups=["delegate"]),
            delegation=AgentDelegationConfig(
                enabled=False, max_parallel=2, timeout_seconds=120, max_tokens=50_000,
                targets=[DelegationTarget(agent="b", remember=True)],
            ),
        )
        again = AgentConfig.model_validate(json.loads(config.model_dump_json()))
        self.assertEqual(again.delegation, config.delegation)
        self.assertEqual(again.delegation.targets[0].remember, True)


class FormRoundTripTests(unittest.TestCase):
    """What the web form saves: the GET view sent back as the PUT body keeps a customised delegation block."""

    def test_the_block_survives_get_then_put_through_the_store(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            user_dir = root / "config" / "user-a"
            (user_dir / "agents").mkdir(parents=True)
            (user_dir / "user.yaml").write_text(
                "id: user-a\nname: User A\nprovider: {name: openrouter}\nagents: [orion, nova]\n", encoding="utf-8",
            )
            (user_dir / "agents" / "orion.yaml").write_text("id: orion\nname: Orion\n", encoding="utf-8")
            (user_dir / "agents" / "nova.yaml").write_text(
                "id: nova\nname: the assistant\nchat_config:\n  tool_groups: [web, delegate]\n"
                "delegation:\n  enabled: true\n  allowed_groups: [web]\n  max_parallel: 2\n  timeout_seconds: 120\n"
                "  targets:\n    - {agent: orion, remember: true}\n", encoding="utf-8",
            )
            from src.config.agents import AgentStore

            store = AgentStore(root / "config", root / "data")
            view = store.load("user-a", "nova").model_dump(mode="json")  # GET /api/agents/nova
            view["description"] = "edited in the form"
            saved = store.update("user-a", "nova", AgentConfig.model_validate(view))  # PUT with that body
            again = store.load("user-a", "nova")
            self.assertEqual(again.description, "edited in the form")
            self.assertEqual(again.delegation, saved.delegation)
            self.assertEqual((again.delegation.max_parallel, again.delegation.timeout_seconds), (2, 120))
            self.assertEqual([(t.agent, t.remember) for t in again.delegation.targets], [("orion", True)])


class RenameAndDeleteTests(AgentTargetFixture):
    def _service_with(self, configs: list[AgentConfig]):
        store = Mock()
        store.list_agents.return_value = configs
        self.service._agents = store
        return store

    def _config(self, *targets: DelegationTarget) -> AgentConfig:
        return AgentConfig(
            id="agent-one", name="One", chat_config=ChatConfig(tool_groups=["delegate"]),
            delegation=AgentDelegationConfig(targets=list(targets)),
        )

    def test_a_rename_follows_the_target(self) -> None:
        store = self._service_with([self._config(DelegationTarget(agent="agent-two", remember=True), DelegationTarget(agent="x"))])
        self.service._retarget_delegation("user-one", "agent-two", "agent-three")
        saved = store.update.call_args.args[2]
        self.assertEqual([(t.agent, t.remember) for t in saved.delegation.targets], [("agent-three", True), ("x", False)])

    def test_a_delete_drops_the_target(self) -> None:
        store = self._service_with([self._config(DelegationTarget(agent="agent-two"), DelegationTarget(agent="x"))])
        self.service._retarget_delegation("user-one", "agent-two", None)
        self.assertEqual([t.agent for t in store.update.call_args.args[2].delegation.targets], ["x"])

    def test_agents_that_do_not_name_it_are_left_alone(self) -> None:
        store = self._service_with([self._config(DelegationTarget(agent="x")), AgentConfig(id="plain", name="P")])
        self.service._retarget_delegation("user-one", "agent-two", None)
        store.update.assert_not_called()

    def test_saving_an_agent_with_an_unknown_target_is_refused(self) -> None:
        with self.assertRaises(InputError):
            self.service.update_agent("user-one", "agent-one", self._config(DelegationTarget(agent="not-mine")))

    def test_the_real_flow_updates_other_agents_on_delete(self) -> None:
        store = self._service_with([self._config(DelegationTarget(agent="agent-two"))])
        store.delete = Mock()
        with patch.object(self.service, "_replace_preferred_agent"):
            self.service.delete_agent("user-one", "agent-two")
        store.delete.assert_called_once()
        self.assertEqual(store.update.call_args.args[2].delegation.targets, [])


if __name__ == "__main__":
    unittest.main()
