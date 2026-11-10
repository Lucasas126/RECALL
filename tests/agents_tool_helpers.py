"""Shared fixture for the `agents` tool tests: the `permissions` fixture plus a caller that holds the `agents` group."""

from __future__ import annotations

import json

from permissions_tool_helpers import PermissionsFixture, Run, resolved

from src.agent.toolsets.agents import build_agent_tools
from src.config.levels import Level
from src.terminal.audit import audit_path
from src.terminal.context import RunContext, run_context

CHIEF = "id: chief\nname: Chief\nlevel: operator\nchat_config:\n  tool_groups: [memory_read, agents]\n"
PNG = b"\x89PNG\r\n\x1a\nnot a real picture, only bytes to move"
READ_TOOLS = ("agent_list", "agent_get", "agent_templates")
CHANGE_TOOLS = ("agent_create", "agent_update", "agent_rename", "agent_delete")
ALL_AGENT_TOOLS = (*READ_TOOLS, *CHANGE_TOOLS)


class AgentsRun(Run):
    """A chat run that also carries the `agents` port, like `ChatService._work` builds it."""

    def context(self) -> RunContext:
        context = super().context()
        context.agents = self.service._agent_admin if self.with_port else None
        return context


class AgentsFixture(PermissionsFixture):
    """user-a (max operator): chief (operator, holds `agents`), boss, bot, ops, box. user-b has `stranger`."""

    def setUp(self) -> None:
        super().setUp()
        self.write_user(self.read("user-a/user.yaml").replace("agents: [boss, bot, ops, box]", "agents: [chief, boss, bot, ops, box]"))
        self.write_agent("chief", CHIEF)

    def tools(self, agent_id: str = "chief", level: Level = Level.OPERATOR, groups=("memory_read", "agents"), user: str = "user-a") -> dict:
        built = build_agent_tools(resolved(user, agent_id, level, list(groups)))
        return {func.tool_spec["function"]["name"]: func for func in built}

    def call(self, name: str, run: Run, /, as_agent: str = "chief", as_level: Level = Level.OPERATOR, **args) -> str:
        with run_context(run.context()):
            return self.tools(as_agent, as_level)[name](**args)

    def run_of(self, answers=(("approve", "once"),), **kwargs) -> AgentsRun:
        return AgentsRun(self.service, "chief", answers, **kwargs)

    def ok(self, tool: str, run: Run, /, **args) -> dict:
        text = self.call(tool, run, **args)
        self.assertFalse(text.startswith("error:"), text)
        return json.loads(text)

    def audit_lines(self, agent_id: str) -> list[dict]:
        path = audit_path(self.data_dir, "user-a", agent_id)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def agent_ids(self) -> list[str]:
        import yaml

        return yaml.safe_load(self.read("user-a/user.yaml"))["agents"]

    def files_of(self, user: str) -> dict:
        return {path.name: path.read_text(encoding="utf-8") for path in (self.config_dir / user).rglob("*.yaml")}
