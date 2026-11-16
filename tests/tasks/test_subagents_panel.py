"""Phases 6 and 7 of the subagents design: the task panel's API and unattended launches."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.tasks import test_subagents_async as base
from tests.support.auth_helpers import login
from tests.support.terminal_helpers import FakeBackend, resolved_multi
from tests.tasks.test_subagents import LIMITS
from tests.tasks.test_subagents_agents import AgentTargetFixture

from src.admin.users import main as admin_main
from src.agent.toolsets.delegate import TaskError
from src.config.models import AgentTerminalBackend, AgentTerminalConfig
from src.web.app import create_app

USER, AGENT = base.USER, base.AGENT


class PanelApiTests(base.AsyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, USER)

    def test_lists_the_tasks_of_every_chat_with_the_parent_names_and_the_budget(self) -> None:
        self.parent_mode = "answer"  # chats with a turn are kept when the next one is created
        list(self.service.send(USER, AGENT, self.chat, "hello"))
        other = self.service.create_chat(USER, AGENT)["id"]
        list(self.service.send(USER, AGENT, other, "hello"))
        self.child_behavior = "ok"
        first = self.launch(wake=False)
        second = self.launch(self.parent(other), wake=False)
        for live in (first, second):
            self.assertTrue(base.until(lambda live=live: live.done.is_set()))
        data = self.client.get("/api/tasks").json()
        self.assertEqual({t["id"] for t in data["tasks"]}, {first.task_id, second.task_id})
        self.assertEqual({t["parent"]["chat_id"] for t in data["tasks"]}, {self.chat, other})
        for task in data["tasks"]:
            self.assertTrue(task["parent"]["agent_name"])
            self.assertIn("title", task["parent"])
            self.assertFalse(task["unattended"])
        # filtered by chat: only that chat's tasks, plus the wake flags
        mine = self.client.get("/api/tasks", params={"agent": AGENT, "chat": other}).json()
        self.assertEqual([t["id"] for t in mine["tasks"]], [second.task_id])
        self.assertIn("pending_wake", mine)

    def test_status_filter_and_limit(self) -> None:
        self.child_behavior = "slow"
        running = self.launch(wake=False)
        # the stub reads `child_behavior` when the worker's model call starts: switch only after the slow one is inside it
        self.assertTrue(base.until(lambda: "".join(running.partial).startswith("RESULT")))
        self.child_behavior = "ok"
        done = self.launch(wake=False)
        self.assertTrue(base.until(lambda: done.done.is_set()))
        ids = lambda **params: [t["id"] for t in self.client.get("/api/tasks", params=params).json()["tasks"]]  # noqa: E731
        self.assertEqual(ids(status="running"), [running.task_id])
        self.assertEqual(ids(status="finished"), [done.task_id])
        self.assertEqual(set(ids(status="")), {running.task_id, done.task_id})
        self.assertEqual(set(ids(status="nonsense")), {running.task_id, done.task_id})
        self.assertEqual(len(ids(limit=1)), 1, "the newest N are kept")
        self.assertEqual(self.client.get("/api/tasks", params={"limit": -1}).status_code, 422)
        self.release_child.set()
        self.assertTrue(base.until(lambda: running.done.is_set()))

    def test_the_list_is_owner_scoped(self) -> None:
        self.child_behavior = "ok"
        live = self.launch(wake=False)
        self.assertTrue(base.until(lambda: live.done.is_set()))
        self.assertEqual(self.manager.list_view("someone-else"), [])
        self.assertEqual(self.service.list_tasks("someone-else")["tasks"], [])



class UnattendedTests(AgentTargetFixture):
    """`launch_unattended` on agent-two (its own SOUL, web only) or an ephemeral worker on agent-one."""

    def setUp(self) -> None:
        super().setUp()
        self.manager.wake_debounce = 0.05

    def unattended(self, agent: str = "agent-two", goal: str = "check the logs", **kwargs):
        return self.manager.launch_unattended("user-one", agent, goal, **kwargs)

    def test_runs_without_a_parent_and_the_result_stays_in_the_record_and_the_list(self) -> None:
        events: list[dict] = []
        self.manager.subscribe(events.append)
        record = self.finish(self.unattended(context="facts", trigger="schedule"))
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["result"]["text"], "AGENT-TWO DONE")
        self.assertTrue(record["unattended"])
        self.assertEqual(record["trigger"], "schedule")
        self.assertEqual(record["parent"], {"agent_id": "agent-two", "chat_id": ""})
        self.assertEqual(record["wake"], {"state": "none"})
        system, message = self.target_calls[0]
        self.assertIn("SOUL OF agent-two", system)
        self.assertRegex(message, r"^(\[[^\]]*\] )?Scheduled task \(schedule\) for user-one")
        self.assertIn("Context:\nfacts", message)
        mark = self.sources[("user-one", "agent-two")].task_mark(record["child"]["chat_id"])
        self.assertTrue(mark.unattended)
        self.assertEqual(mark.kind, "agent")
        self.assertEqual(events, [], "nobody listens for a task without a parent chat")
        listed = self.service.list_tasks("user-one")["tasks"]
        self.assertEqual([t["id"] for t in listed], [record["id"]])
        self.assertTrue(listed[0]["unattended"])
        self.assertEqual(listed[0]["parent"]["title"], "")
        self.assertEqual(listed[0]["parent"]["agent_name"], "Name of agent-two")
        self.assertEqual(self.service.get_task_chat("user-one", record["id"])["task"]["agent_id"], "agent-two")

    def test_the_result_is_delivered_to_the_notify_chat_as_a_wake(self) -> None:
        live = self.unattended(notify_chat=("agent-one", self.chat))
        record = self.finish(live)
        self.assertEqual(record["parent"], {"agent_id": "agent-one", "chat_id": self.chat})
        source = self.sources[("user-one", "agent-one")]
        self.assertTrue(self.wait_until(lambda: any(t.get("source") == "task" for t in source.load(self.chat))), "a wake turn arrived")
        wake = next(t for t in source.load(self.chat) if t.get("source") == "task")
        self.assertIn(record["id"], wake["task_ids"])
        self.assertIn("AGENT-TWO DONE", wake["content"])

    def wait_until(self, check, timeout: float = 8.0) -> bool:
        return base.until(check, timeout)

    def test_an_ephemeral_worker_runs_on_the_agents_model_with_instructions(self) -> None:
        live = self.unattended("agent-one", instructions="You are a poet.", groups=["web", "memory_edit"])
        record = self.finish(live)
        self.assertEqual(record["status"], "done")
        self.assertEqual(record["result"]["text"], "RESULT for: check the logs")
        self.assertEqual(record["target"]["kind"], "ephemeral")
        self.assertEqual(record["grant"], {"groups": ["web"], "backends": []}, "only grantable groups, never memory")
        self.assertIn("poet", self.child_systems[0])
        self.assertTrue(self.sources[("user-one", "agent-one")].task_mark(record["child"]["chat_id"]).unattended)

    def finish(self, live) -> dict:
        self.assertTrue(live.done.wait(8))
        return self.record(live.task_id)

    def test_the_same_limits_apply(self) -> None:
        with self.assertRaises(TaskError):
            self.unattended(goal="   ")
        with self.assertRaises(TaskError) as caught:
            self.unattended(timeout_s=10_000)
        self.assertIn("timeout_s", str(caught.exception))
        self.target_hold = True
        live = [self.unattended(goal=f"job {i}") for i in range(5)]  # per-parent limit of 5 (the parent is the agent itself)
        with self.assertRaises(TaskError) as caught:
            self.unattended(goal="one too many")
        self.assertIn("limit reached", str(caught.exception))
        self.release_child.set()
        for item in live:
            self.finish(item)

    def test_refused_for_an_agent_of_another_user_or_an_unknown_one(self) -> None:
        for user, agent in (("user-one", "nobody"), ("nobody", "agent-two")):
            with self.subTest(user=user, agent=agent):
                with self.assertRaises(TaskError):
                    self.manager.launch_unattended(user, agent, "x")
        with self.assertRaises(TaskError):
            self.unattended(notify_chat=("agent-one", "no-such-chat"))
        with self.assertRaises(TaskError):
            self.manager.launch_unattended("user-one", "agent-two", "x", notify_chat=("nobody", self.chat))
        self.assertEqual(self.manager.store.list("user-one"), [])

    def test_the_rate_limit_cooldown_applies(self) -> None:
        self.finish(self.unattended())
        self.manager.store.block_launches("user-one", 300)
        with self.assertRaises(TaskError) as caught:
            self.unattended()
        self.assertIn("rate limit", str(caught.exception))

    def test_a_worker_bound_to_an_unattended_task_is_not_interactive(self) -> None:
        from src.providers.cancellation import CancellationToken
        from src.terminal.context import RunContext

        record = self.finish(self.unattended())
        mark = self.sources[("user-one", "agent-two")].task_mark(record["child"]["chat_id"])
        context = RunContext("user-one", "agent-two", record["child"]["chat_id"], None, lambda *a: None, CancellationToken())
        self.manager.bind_worker(context, mark)
        self.assertFalse(context.interactive)
        normal = self.finish(self.to_agent())
        normal_mark = self.sources[("user-one", "agent-two")].task_mark(normal["child"]["chat_id"])
        context = RunContext("user-one", "agent-two", normal["child"]["chat_id"], None, lambda *a: None, CancellationToken())
        self.manager.bind_worker(context, normal_mark)
        self.assertTrue(context.interactive)


class UnattendedTerminalTests(AgentTargetFixture):
    """agent-two has a terminal backend `box`; the unattended worker runs a command through the scripted model."""

    target_groups = ["web", "terminal"]
    box_mode = "manual"
    term_command = "ls"

    def setUp(self) -> None:
        self.backend = FakeBackend()
        super().setUp()
        self.target_terminal = AgentTerminalConfig(backends={"box": AgentTerminalBackend(approvals=self.box_mode)})
        for patcher in (
            patch("src.agent.toolsets.terminal.make_backend", side_effect=lambda name, cfg: self.backend),
            patch("src.agent.toolsets.terminal.DATA_DIR", self.data),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _factory(self, user, agent, chat_id):
        instance = super()._factory(user, agent, chat_id)
        if agent == "agent-two":
            template = resolved_multi(box=(self.box_mode, "~/workspace"))
            instance.config.terminal = template.terminal
            instance.config.terminal_backends = template.terminal_backends
            instance.config.tool_groups = list(self.target_groups)
            instance.config.tools = True
            if instance._task is not None:
                instance._become_worker(instance._task)
            instance._base_tool_specs, instance._base_tool_handlers = instance._build_base_tools()
            instance._tool_specs, instance._tool_handlers = list(instance._base_tool_specs), dict(instance._base_tool_handlers)
            instance.messages[0]["content"] = instance._system_prompt()
        return instance

    def _script(self, key):
        parent = super()._script(key)

        def respond(messages, model, **kwargs):
            if key[1] == "agent-two" and "terminal" in str(kwargs.get("tools") or ""):
                usage = {"prompt_tokens": 10, "completion_tokens": 5}
                if messages[-1]["role"] == "tool":
                    yield "content", "TERMINAL: " + messages[-1]["content"][:120]
                    yield {"usage": usage}
                else:
                    yield {"tool_calls": [{"id": "c1", "name": "terminal", "arguments": {"command": self.term_command, "backend": "box"}}],
                           "usage": usage}
                return
            yield from parent(messages, model, **kwargs)

        return respond

    def run_unattended(self, mode: str, command: str = "ls") -> tuple[dict, FakeBackend]:
        self.box_mode, self.term_command = mode, command
        self.target_terminal = AgentTerminalConfig(backends={"box": AgentTerminalBackend(approvals=mode)})
        live = self.manager.launch_unattended("user-one", "agent-two", "list the files")
        self.assertTrue(live.done.wait(8))
        self.assertEqual(self.service._approvals.pending_for_task("user-one", live.task_id), 0, "nobody is asked")
        return self.record(live.task_id), self.backend

    def test_manual_is_denied_without_asking_anyone(self) -> None:
        record, backend = self.run_unattended("manual")
        self.assertEqual(record["status"], "done")
        self.assertIn("Denied", record["result"]["text"])
        self.assertEqual(backend.calls, [])
        self.assertEqual(record["grant"], {"groups": ["web", "terminal"], "backends": ["box"]})

    def test_off_is_never_inherited_so_it_is_denied_too(self) -> None:
        record, backend = self.run_unattended("off")
        self.assertIn("Denied", record["result"]["text"])
        self.assertEqual(backend.calls, [])

    def test_smart_stays_automatic_for_a_normal_command(self) -> None:
        record, backend = self.run_unattended("smart")
        self.assertNotIn("Denied", record["result"]["text"])
        self.assertEqual([c.command for c in backend.calls], ["ls"])

    def test_smart_still_denies_a_dangerous_command(self) -> None:
        record, backend = self.run_unattended("smart", "rm -rf build")
        self.assertIn("Denied", record["result"]["text"])
        self.assertEqual(backend.calls, [])

    def test_a_normal_task_to_the_same_agent_is_always_manual(self) -> None:
        self.box_mode = "smart"
        self.target_terminal = AgentTerminalConfig(backends={"box": AgentTerminalBackend(approvals="smart")})
        source = self.sources.get(("user-one", "agent-two")) or self.service._conversations("user-one", "agent-two")
        from tests.tasks.test_subagents import mark

        chat = source.create(origin="task", task=mark("cccccccccccc", ["terminal"]).model_copy(update={"backends": ["box"], "kind": "agent"}))
        worker = self._factory("user-one", "agent-two", chat)
        self.assertEqual({n: e.approvals for n, e in worker.config.terminal.backends.items()}, {"box": "manual"})
        chat = source.create(origin="task", task=mark("dddddddddddd", ["terminal"]).model_copy(update={"backends": ["box"], "kind": "agent", "unattended": True}))
        worker = self._factory("user-one", "agent-two", chat)
        self.assertEqual({n: e.approvals for n, e in worker.config.terminal.backends.items()}, {"box": "smart"})


class AdminCliTests(AgentTargetFixture):
    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with patch("src.admin.tasks._make_service", return_value=self.service), redirect_stdout(out), redirect_stderr(err):
            code = admin_main(["task", "run", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_wait_prints_the_id_and_the_result(self) -> None:
        code, out, err = self.run_cli("--user", "user-one", "--agent", "agent-two", "--goal", "summarise", "--wait")
        self.assertEqual((code, err), (0, ""))
        task_id = out.split("task: ", 1)[1].split()[0]
        record = self.manager.store.get("user-one", task_id)
        self.assertEqual((record["status"], record["trigger"], record["unattended"]), ("done", "admin", True))
        self.assertIn("status: done", out)
        self.assertIn("tokens: 15", out)
        self.assertIn("result:\nAGENT-TWO DONE", out)

    def test_without_wait_it_only_prints_the_id(self) -> None:
        code, out, err = self.run_cli("--user", "user-one", "--agent", "agent-two", "--goal", "x")
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"^task: [0-9a-f]{12}$")
        self.assertIn("--wait", err)
        self.assertTrue(base.until(lambda: not self.manager._live))

    def test_errors_are_printed_and_exit_nonzero(self) -> None:
        code, out, err = self.run_cli("--user", "user-one", "--agent", "nobody", "--goal", "x")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("error: agent 'nobody' is not available", err)
        code, _, err = self.run_cli("--user", "user-one", "--agent", "agent-two", "--goal", "x", "--notify", "bad")
        self.assertEqual(code, 1)
        self.assertIn("AGENT:CHAT", err)

    def test_notify_delivers_the_result_to_the_chat(self) -> None:
        self.manager.wake_debounce = 0.05
        code, out, _ = self.run_cli(
            "--user", "user-one", "--agent", "agent-two", "--goal", "x", "--notify", f"agent-one:{self.chat}", "--wait",
        )
        self.assertEqual(code, 0)
        turns = self.sources[("user-one", "agent-one")].load(self.chat)
        self.assertTrue(any(t.get("source") == "task" for t in turns))


if __name__ == "__main__":
    unittest.main()
