"""Phase 3 of the subagents design: a worker's approvals are asked in the PARENT chat; terminal grants for workers."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from unittest.mock import Mock, patch

from terminal_helpers import FakeBackend, Responder, make_session, resolved_multi
from test_subagents import LIMITS, SubagentFixture, mark

from src.agent.agent import Agent
from src.agent.toolsets.delegate import TaskError, build_delegate_tools, granted_backends
from src.config.models import AgentDelegationConfig, AgentTerminalBackend, AgentTerminalConfig, TerminalSettings
from src.providers.cancellation import CancellationToken
from src.service.tasks import _LiveTask
from src.terminal.approvals import ApprovalBroker
from src.terminal.backends import LocalBackend
from src.terminal.context import RunContext, run_context
from src.terminal.session import TerminalSession

TASK = {"id": "0123456789ab", "title": "Check the logs", "kind": "ephemeral", "worker": "ephemeral"}


def child_context(broker: ApprovalBroker, responder: Responder, *, interactive: bool = True, waits=None) -> RunContext:
    """The run context of a worker: its own chat is `kid`, its approvals go to the parent chat `parent`."""
    context = RunContext("u", "a", "kid", broker, responder.emit, responder.cancellation, interactive=interactive)
    context.approval_target = ("a", "parent")
    context.task = dict(TASK)
    context.approval_emit = responder.emit
    if waits is not None:
        context.approval_wait = waits.append
    return context


class Runner:
    """Runs a command through a session in a thread, as the worker's tool thread would."""

    def __init__(self, session: TerminalSession, context: RunContext, command: str = "ls"):
        self.result: tuple = (None, None)

        def go() -> None:
            with run_context(context):
                self.result = session.run("terminal", command, command)

        self.thread = threading.Thread(target=go, daemon=True)
        self.thread.start()

    def wait_pending(self, broker: ApprovalBroker, chat: str = "parent") -> dict:
        deadline = monotonic() + 3
        while not (found := broker.pending("u", "a", chat)):
            assert monotonic() < deadline, "no approval became pending"
            sleep(0.01)
        return found[0]

    def join(self) -> tuple:
        self.thread.join(5)
        assert not self.thread.is_alive()
        return self.result


class ChildApprovalBrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = ApprovalBroker(5)
        self.addCleanup(self.broker.close)
        self.responder = Responder(self.broker, answer=None)

    def start(self, approvals: str = "manual", **kwargs) -> tuple[Runner, dict]:
        runner = Runner(make_session(approvals=approvals), child_context(self.broker, self.responder, **kwargs))
        return runner, runner.wait_pending(self.broker)

    def test_the_request_is_listed_under_the_parent_chat_and_attributed(self) -> None:
        runner, pending = self.start()
        self.assertEqual((pending["agent_id"], pending["chat_id"]), ("a", "parent"))
        self.assertEqual(pending["task"], TASK)
        self.assertEqual(self.broker.pending("u", "a", "kid"), [], "nothing is asked in the worker's own hidden chat")
        self.assertEqual(self.broker.pending_for_task("u", TASK["id"]), 1)
        self.assertEqual(self.responder.events[0][1]["task"]["title"], "Check the logs")
        self.broker.resolve(pending["id"], "u", "deny")
        runner.join()

    def test_approve_resumes_the_worker(self) -> None:
        runner, pending = self.start()
        self.assertEqual(self.broker.resolve(pending["id"], "u", "approve"), "approved")
        result, denial = runner.join()
        self.assertIsNone(denial)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.broker.pending_for_task("u", TASK["id"]), 0)

    def test_deny_refuses_the_command(self) -> None:
        runner, pending = self.start()
        self.broker.resolve(pending["id"], "u", "deny")
        result, denial = runner.join()
        self.assertIsNone(result)
        self.assertIn("did not approve", denial)

    def test_only_the_owner_resolves_it(self) -> None:
        from src.terminal.approvals import ApprovalForbidden

        runner, pending = self.start()
        with self.assertRaises(ApprovalForbidden):
            self.broker.resolve(pending["id"], "someone-else", "approve")
        self.broker.resolve(pending["id"], "u", "deny")
        runner.join()

    def test_an_unanswered_approval_times_out_and_denies(self) -> None:
        broker = ApprovalBroker(0.4)
        self.addCleanup(broker.close)
        runner = Runner(make_session(), child_context(broker, Responder(broker, answer=None)))
        runner.wait_pending(broker)
        result, denial = runner.join()
        self.assertIsNone(result)
        self.assertIn("timed out", denial)

    def test_the_run_is_told_while_it_waits(self) -> None:
        waits: list[bool] = []
        runner, pending = self.start(waits=waits)
        self.assertEqual(waits, [True])
        self.broker.resolve(pending["id"], "u", "approve")
        runner.join()
        self.assertEqual(waits, [True, False])

    def test_events_go_to_the_approval_emitter_of_the_run(self) -> None:
        routed: list[tuple[str, dict]] = []
        context = child_context(self.broker, self.responder)
        context.approval_emit = lambda kind, data: routed.append((kind, data))
        runner = Runner(make_session(), context)
        pending = runner.wait_pending(self.broker)
        self.broker.resolve(pending["id"], "u", "approve")
        runner.join()
        self.assertEqual([kind for kind, _ in routed], ["approval", "approval_resolved"])
        self.assertEqual(self.responder.events, [], "the worker's own stream does not carry them")

    def test_unattended_denies_manual_and_dangerous_but_smart_auto_stays_auto(self) -> None:
        context = child_context(self.broker, self.responder, interactive=False)
        with run_context(context):
            result, denial = make_session(approvals="manual").run("terminal", "ls", "ls")
            self.assertIsNone(result)
            self.assertIn("nobody is attached", denial)
            result, denial = make_session(approvals="smart").run("terminal", "ls", "ls")
            self.assertIsNone(denial)
            self.assertIn("nobody attached", result.approval)
            result, denial = make_session(approvals="smart").run("terminal", "rm -rf build", "rm -rf build")
            self.assertIsNone(result)
            self.assertIn("dangerous", denial)
        self.assertEqual(self.broker.pending("u", "a", "parent"), [])


class ProtectedPathTests(unittest.TestCase):
    def test_protected_paths_are_denied_on_local_in_every_mode_for_a_worker(self) -> None:
        broker = ApprovalBroker(2)
        self.addCleanup(broker.close)
        responder = Responder(broker, answer="approve")  # even a user who would approve cannot get past it
        for mode in ("manual", "smart", "off"):
            for interactive in (True, False):
                with self.subTest(mode=mode, interactive=interactive):
                    session = TerminalSession(
                        FakeBackend(), AgentTerminalConfig(backends={"box": AgentTerminalBackend(approvals=mode, workdir="~/workspace")}),
                        TerminalSettings(), user_id="u", agent_id="a", name="box", local=True,
                    )
                    with run_context(child_context(broker, responder, interactive=interactive)):
                        result, denial = session.run("terminal", "cat ~/.ssh/id_rsa", "cat ~/.ssh/id_rsa")
                    self.assertIsNone(result)
                    self.assertIn("protected path", denial)
        self.assertEqual(responder.events, [], "a protected path is never even asked about")


class TimerPauseTests(unittest.TestCase):
    def live(self) -> _LiveTask:
        return _LiveTask("u", "t", "a", "kid", LIMITS, 100, Mock())

    def test_time_awaiting_an_approval_does_not_count(self) -> None:
        live = self.live()
        clock = [1000.0]
        with patch("src.service.tasks.monotonic", lambda: clock[0]):
            live.started = 1000.0
            clock[0] = 1010.0
            self.assertAlmostEqual(live.active_seconds(), 10.0)
            live.approval_wait(True)
            clock[0] = 1090.0  # 80 s waiting for the user
            self.assertAlmostEqual(live.active_seconds(), 10.0, msg="paused while waiting")
            live.approval_wait(False)
            clock[0] = 1095.0
            self.assertAlmostEqual(live.active_seconds(), 15.0, msg="resumes after the decision")

    def test_overlapping_waits_pause_once(self) -> None:
        live = self.live()
        clock = [0.0]
        with patch("src.service.tasks.monotonic", lambda: clock[0]):
            live.started = 0.0
            live.approval_wait(True)
            clock[0] = 10.0
            live.approval_wait(True)
            clock[0] = 20.0
            live.approval_wait(False)
            clock[0] = 30.0
            self.assertAlmostEqual(live.active_seconds(), 0.0)
            live.approval_wait(False)
            clock[0] = 35.0
            self.assertAlmostEqual(live.active_seconds(), 5.0)


class TerminalGrantFixture(SubagentFixture):
    """A parent with the terminal (`box` on off, `lab` on smart, `local` on smart) and a worker that runs `ls`."""

    groups = ["delegate", "web", "terminal"]

    def setUp(self) -> None:
        super().setUp()
        self.backend = FakeBackend()
        self.worker_commands: list[str] = []
        for patcher in (
            patch("src.agent.toolsets.terminal.make_backend", side_effect=lambda name, cfg: self.backend),
            patch("src.agent.toolsets.terminal.DATA_DIR", self.data),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        base = self.providers[("user-one", "agent-one")].invoke.side_effect

        def script(messages, model, **kwargs):
            if "worker agent" in messages[0]["content"] and "terminal" in str(kwargs.get("tools") or ""):
                if messages[-1]["role"] == "tool":
                    yield "content", "RESULT terminal: " + messages[-1]["content"][:60]
                    yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                else:
                    yield {"tool_calls": [{"id": "c1", "name": "terminal", "arguments": {"command": "ls", "backend": "box"}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
                return
            yield from base(messages, model, **kwargs)

        self.providers[("user-one", "agent-one")].invoke.side_effect = script

    def _factory(self, user, agent, chat_id):
        instance = super()._factory(user, agent, chat_id)
        template = resolved_multi(box=("off", "~/workspace"), lab=("smart", "~/workspace"))
        instance.config.terminal = template.terminal
        instance.config.terminal_backends = template.terminal_backends
        instance.config.tool_groups = list(self.groups)
        instance.config.tools = True
        # rebuild the tools with the terminal config now in place
        if instance._task is not None:
            instance._become_worker(instance._task)
        instance._base_tool_specs, instance._base_tool_handlers = instance._build_base_tools()
        instance._tool_specs, instance._tool_handlers = list(instance._base_tool_specs), dict(instance._base_tool_handlers)
        instance.messages[0]["content"] = instance._system_prompt()
        return instance

    def parent_config(self):
        config = resolved_multi(box=("off", "~/workspace"), lab=("smart", "~/workspace"))
        return config.model_copy(update={"user_id": "user-one", "id": "agent-one", "tool_groups": ["delegate", "terminal"],
                                         "delegation": AgentDelegationConfig()})

    def launch_with(self, backends: list[str], **overrides):
        return self.launch(backends=backends, grantable_backends=["box", "lab"], **overrides)

    def wait_for(self, check, what: str):
        deadline = monotonic() + 8
        while monotonic() < deadline:
            if value := check():
                return value
            sleep(0.02)
        self.fail(f"timed out waiting for {what}")


class GrantTests(TerminalGrantFixture):
    def test_the_tool_offers_only_the_backends_the_parent_has(self) -> None:
        config = self.parent_config()
        self.assertEqual(granted_backends(config), ["box", "lab"])
        spec = build_delegate_tools(config)[0].tool_spec["function"]
        self.assertEqual(spec["parameters"]["properties"]["backends"]["items"]["enum"], ["box", "lab"])
        self.assertIn("approval", spec["description"])
        no_terminal = config.model_copy(update={"tool_groups": ["delegate"]})
        self.assertEqual(granted_backends(no_terminal), [])
        self.assertNotIn("enum", build_delegate_tools(no_terminal)[0].tool_spec["function"]["parameters"]["properties"]["backends"]["items"])

    def test_a_backend_the_parent_lacks_is_refused(self) -> None:
        with self.assertRaises(TaskError) as caught:
            self.launch(backends=["prod"], grantable_backends=["box"])
        self.assertIn("cannot grant backend prod", str(caught.exception))
        with self.assertRaises(TaskError):
            self.launch(backends=["box"], grantable_backends=[])  # a parent without the terminal group
        self.assertEqual(self.manager.store.list("user-one"), [])

    def test_naming_a_backend_grants_the_terminal_group_and_is_recorded(self) -> None:
        live = self.launch_with(["box"])
        self.addCleanup(lambda: (self.manager.stop("user-one", live.task_id), live.done.wait(8)))  # it waits for an approval
        record = self.record(live.task_id)
        self.assertEqual(record["grant"], {"groups": ["terminal"], "backends": ["box"]})
        source = self.sources[("user-one", "agent-one")]
        chat = record["child"]["chat_id"]
        task = source.task_mark(chat)
        self.assertEqual((task.groups, task.backends, task.title), (["terminal"], ["box"], record["title"]))

    def test_a_worker_never_gets_a_looser_mode_than_manual(self) -> None:
        source = self.sources[("user-one", "agent-one")]
        child = source.create(origin="task", task=mark("aaaaaaaaaaaa", ["terminal"]).model_copy(update={"backends": ["box", "lab", "prod"]}))
        worker = self._factory("user-one", "agent-one", child)
        modes = {name: entry.approvals for name, entry in worker.config.terminal.backends.items()}
        self.assertEqual(modes, {"box": "manual", "lab": "manual"}, "off and smart become manual; `prod` (not the parent's) is dropped")
        self.assertEqual(sorted(worker.config.terminal_backends), ["box", "lab"])
        self.assertEqual(worker.config.terminal.default_backend, "box")
        # the parent's own configuration is untouched
        parent = self._factory("user-one", "agent-one", self.chat)
        self.assertEqual({n: e.approvals for n, e in parent.config.terminal.backends.items()}, {"box": "off", "lab": "smart"})

    def test_a_worker_chat_keeps_its_terminal_while_excluded_from_learning(self) -> None:
        source = self.sources[("user-one", "agent-one")]
        granted = source.create(origin="task", task=mark("aaaaaaaaaaaa", ["terminal"]).model_copy(update={"backends": ["box"]}))
        plain = source.create(origin="task", task=mark("bbbbbbbbbbbb", []))
        disposable = source.create(disposable=True)
        worker = self._factory("user-one", "agent-one", granted)
        self.assertTrue(source.is_disposable(granted), "still excluded from learning")
        self.assertTrue(worker._disposable)
        self.assertEqual(sorted(worker._tool_handlers), ["sandbox_patch", "sandbox_read", "sandbox_search", "sandbox_write", "terminal"])
        for blocked in ("memory", "memory_edit", "skill_manage", "soul_edit", "task_start"):
            self.assertNotIn(blocked, worker._tool_handlers)
        self.assertEqual(self._factory("user-one", "agent-one", plain)._tool_handlers, {}, "no grant, no terminal")
        self.assertNotIn("terminal", self._factory("user-one", "agent-one", disposable)._tool_handlers, "a plain disposable chat stays blocked")
        self.assertIn("`box`", worker.messages[0]["content"], "the notice describes the granted backend")
        self.assertNotIn("`lab`", worker.messages[0]["content"])


class EndToEndApprovalTests(TerminalGrantFixture):
    def test_the_approval_of_a_worker_appears_in_the_parent_chat_and_resolving_resumes_it(self) -> None:
        events: list[dict] = []
        self.manager.subscribe(events.append)
        live = self.launch_with(["box"], timeout_s=60)
        pending = self.wait_for(lambda: self.service.list_approvals("user-one", "agent-one", self.chat), "an approval")[0]
        self.assertEqual((pending["agent_id"], pending["chat_id"]), ("agent-one", self.chat))
        self.assertEqual(pending["task"]["id"], live.task_id)
        self.assertEqual((pending["task"]["kind"], pending["task"]["worker"]), ("ephemeral", "ephemeral"))
        self.assertEqual(pending["backend"], "box")
        self.assertEqual(pending["reason"], "approvals are set to manual", "box is `off` for the parent, manual for the worker")
        # the task card derives 'awaiting approval' from the broker
        views = self.manager.list_view("user-one", "agent-one", self.chat)
        self.assertEqual(views[0]["awaiting_approval"], 1)
        self.assertEqual(views[0]["backends"], ["box"])
        kinds = [e["kind"] for e in events]
        self.assertIn("task_approval", kinds)
        card = next(e for e in events if e["kind"] == "task_approval")
        self.assertEqual((card["agent_id"], card["chat_id"], card["data"]["id"]), ("agent-one", self.chat, pending["id"]))
        # the worker's own hidden chat shows nothing
        child = self.record(live.task_id)["child"]["chat_id"]
        self.assertEqual(self.service._approvals.pending("user-one", "agent-one", child), [])
        # another user cannot resolve it
        from src.terminal.approvals import ApprovalForbidden

        with self.assertRaises(ApprovalForbidden):
            self.service.resolve_approval("user-two", "agent-one", self.chat, pending["id"], "approve")
        self.assertEqual(self.service.resolve_approval("user-one", "agent-one", self.chat, pending["id"], "approve"), {"status": "approved", "scope": "once"})
        live.done.wait(8)
        record = self.record(live.task_id)
        self.assertEqual(record["status"], "done")
        self.assertIn("RESULT terminal", record["result"]["text"])
        self.assertIn("task_approval_resolved", [e["kind"] for e in events])
        self.assertEqual(self.manager.list_view("user-one", "agent-one", self.chat)[0]["awaiting_approval"], 0)

    def test_denying_refuses_the_command_and_the_worker_reports_it(self) -> None:
        live = self.launch_with(["box"], timeout_s=60)
        pending = self.wait_for(lambda: self.service.list_approvals("user-one", "agent-one", self.chat), "an approval")[0]
        self.service.resolve_approval("user-one", "agent-one", self.chat, pending["id"], "deny")
        live.done.wait(8)
        self.assertIn("Denied", self.record(live.task_id)["result"]["text"])

    def test_the_timeout_is_paused_while_the_worker_waits_for_the_user(self) -> None:
        live = self.launch_with(["box"], timeout_s=1)
        pending = self.wait_for(lambda: self.service.list_approvals("user-one", "agent-one", self.chat), "an approval")[0]
        sleep(1.6)  # longer than the task's whole timeout
        self.assertFalse(live.done.is_set(), "still waiting for the user, not timed out")
        self.assertEqual(self.record(live.task_id)["status"], "running")
        self.service.resolve_approval("user-one", "agent-one", self.chat, pending["id"], "approve")
        live.done.wait(8)
        self.assertEqual(self.record(live.task_id)["status"], "done")

    def test_stopping_the_task_denies_its_pending_approval(self) -> None:
        live = self.launch_with(["box"], timeout_s=60)
        self.wait_for(lambda: self.service.list_approvals("user-one", "agent-one", self.chat), "an approval")
        self.manager.stop("user-one", live.task_id)
        live.done.wait(8)
        self.assertEqual(self.service.list_approvals("user-one", "agent-one", self.chat), [])
        self.assertEqual(self.record(live.task_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
