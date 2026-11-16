"""Shared fixture for the `permissions` tool tests: a user with several agents, a real ChatService and a card-answering run."""

from __future__ import annotations

import json
import os
from unittest.mock import patch

from tests.support.admin_helpers import USER_YAML, AdminFixture
from tests.support.auth_helpers import TEST_PASSWORD_HASH

from src.agent.toolsets.permissions import build_permission_tools
from src.config.levels import Level
from src.config.models import ModelsConfig, ResolvedAgent
from src.providers.cancellation import CancellationToken
from src.terminal.audit import audit_path
from src.terminal.context import RunContext, run_context

BACKENDS = (
    "terminal_backends:\n"
    "  sandbox: {type: ssh, kind: sandbox, host: a.example, user: deploy, key_file: /k, known_hosts_file: /h}\n"
    "  pc: {type: ssh, host: b.example, user: user-a, key_file: /k, known_hosts_file: /h}\n"
    "  local: {type: local, workdir: /app}\n"
)
BOSS = "id: boss\nname: Boss\nlevel: operator\nchat_config:\n  tool_groups: [memory_read, permissions]\n"
BOT = "id: bot\nname: Bot\nlevel: assistant\nchat_config:\n  tool_groups: [memory_read, memory_edit]\n"
BOT_GROUPS = ["memory_read", "memory_edit"]
MORE_GROUPS = ["memory_read", "memory_edit", "web"]  # a change of bot that its level (assistant) allows
OPS = (
    "id: ops\nname: Ops\nlevel: operator\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: pc\n"
    "  backends: {pc: {}}\n"
)
SANDBOXED = (
    "id: box\nname: Box\nlevel: operator\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: sandbox\n"
    "  backends: {sandbox: {}}\n"
)
OTHER_USER = (
    "id: user-b\nname: User B\nmax_level: admin\nprovider: {name: ollama}\nagents: [stranger]\n"
    "password_hash: '%s'\n" % TEST_PASSWORD_HASH
)
STRANGER = "id: stranger\nname: Stranger\nlevel: admin\nchat_config:\n  tool_groups: [memory_read]\n"


def resolved(user_id: str, agent_id: str, level: Level, groups: list[str]) -> ResolvedAgent:
    return ResolvedAgent(
        user_id=user_id, id=agent_id, name=agent_id.title(), models=ModelsConfig(), provider_name="ollama", api_key="",
        embeddings_provider_name="ollama", embeddings_api_key="", tools=True, tool_groups=groups, level=level,
    )


class Run:
    """A chat run of one agent: answers each approval card through the service like the web API (answers consumed in order)."""

    def __init__(self, service, agent_id: str = "boss", answers=(("approve", "once"),), *, interactive: bool = True,
                 user: str = "user-a", chat_id: str = "chat1", with_port: bool = True, silent: bool = False):
        self.service, self.agent_id, self.user, self.chat_id = service, agent_id, user, chat_id
        self.answers = list(answers)
        self.interactive, self.with_port, self.silent = interactive, with_port, silent  # silent: nobody answers
        self.events: list[tuple[str, dict]] = []
        self.results: list = []
        self.errors: list[Exception] = []

    def emit(self, kind: str, data: dict) -> None:
        self.events.append((kind, data))
        if kind != "approval" or self.silent:
            return
        decision, scope = self.answers.pop(0) if self.answers else ("deny", "once")
        try:
            self.results.append(self.service.resolve_approval(self.user, self.agent_id, self.chat_id, data["id"], decision, scope))
        except Exception as exc:  # the broker refuses a scope the card did not offer; the card then stays open
            self.errors.append(exc)
            self.results.append(self.service.resolve_approval(self.user, self.agent_id, self.chat_id, data["id"], "deny", "once"))

    @property
    def cards(self) -> list[dict]:
        return [data for kind, data in self.events if kind == "approval"]

    def context(self) -> RunContext:
        return RunContext(
            self.user, self.agent_id, self.chat_id, self.service._approvals, self.emit, CancellationToken(),
            interactive=self.interactive, permissions=self.service._permissions if self.with_port else None,
        )


class PermissionsFixture(AdminFixture):
    """user-a (max_level operator by default): boss (operator, holds `permissions`), bot, ops (real machine), box (sandbox)."""

    MAX_LEVEL = "operator"

    def setUp(self) -> None:
        super().setUp()
        environment = patch.dict(os.environ, {"USER_A_KEY_NAME": "secret"})
        environment.start()
        self.addCleanup(environment.stop)
        self.write_user(
            USER_YAML.format(hash=TEST_PASSWORD_HASH).replace("max_level: admin", f"max_level: {self.MAX_LEVEL}")
            .replace("agents: [bot]", "agents: [boss, bot, ops, box]") + BACKENDS
        )
        for name, text in (("boss", BOSS), ("bot", BOT), ("ops", OPS), ("box", SANDBOXED)):
            self.write_agent(name, text)
        (self.config_dir / "user-b" / "agents").mkdir(parents=True)
        (self.config_dir / "user-b" / "user.yaml").write_text(OTHER_USER, encoding="utf-8")
        (self.config_dir / "user-b" / "agents" / "stranger.yaml").write_text(STRANGER, encoding="utf-8")

    # -------------------------------------------------------------- helpers
    def tools(self, agent_id: str = "boss", level: Level = Level.OPERATOR, groups=("memory_read", "permissions"),
              user: str = "user-a") -> dict:
        built = build_permission_tools(resolved(user, agent_id, level, list(groups)))
        return {func.tool_spec["function"]["name"]: func for func in built}

    def call(self, name: str, run: Run, /, as_agent: str = "boss", as_level: Level = Level.OPERATOR, **args) -> str:
        tools = self.tools(as_agent, as_level)
        with run_context(run.context()):
            return tools[name](**args)

    def stored(self, agent_id: str) -> dict:
        import yaml

        return yaml.safe_load(self.read(f"user-a/agents/{agent_id}.yaml"))

    def audit_events(self, agent_id: str) -> list[dict]:
        path = audit_path(self.data_dir, "user-a", agent_id)
        if not path.exists():
            return []
        lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if line.get("event") == "permission_change"]
