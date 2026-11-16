"""A temporary config/data tree plus a real ChatService (no providers) for the admin CLI and pickup tests."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.support.auth_helpers import TEST_PASSWORD_HASH

from src.admin import users
from src.service.chat import ChatService

CONFIG_TARGETS = (
    "src.admin.users.CONFIG_DIR", "src.admin.agents.CONFIG_DIR", "src.admin.credentials.CONFIG_DIR",
    "src.config.loader.CONFIG_DIR", "src.service.chat.CONFIG_DIR", "src.config.settings.CONFIG_DIR",
)
DATA_TARGETS = (
    "src.admin.agents.DATA_DIR", "src.config.loader.DATA_DIR", "src.service.chat.DATA_DIR",
    "src.core.sources.registry.DATA_DIR", "src.agent.toolsets.secrets.DATA_DIR",
)
USER_YAML = "id: user-a\nname: User A\nmax_level: admin\nprovider: {{name: ollama, api_key: USER_A_KEY_NAME}}\nagents: [bot]\npassword_hash: '{hash}'\n"


class FakeAgent:
    """Just enough of Agent for ChatService._run; its reply names the config it was built from."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.config = SimpleNamespace(language="en")
        self.last_actions: list = []
        self.last_usage = self.last_latency_report = self.last_reply = self.last_reasoning = ""

    def refresh_user_models(self, *args) -> None:
        pass

    def memory_event(self):
        return None

    def invoke(self, text, **kwargs):
        return f"reply from {self.label}"


class AdminFixture(unittest.TestCase):
    """`self.config_dir` / `self.data_dir` are temp folders; patches cover every module that reads them."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_dir, self.data_dir = self.root / ".config", self.root / "data"
        (self.config_dir / "user-a" / "agents").mkdir(parents=True)
        self.data_dir.mkdir()
        self.write_user()
        self.write_agent("bot", "id: bot\nname: Bot\n")
        for target, value in [*((t, self.config_dir) for t in CONFIG_TARGETS), *((t, self.data_dir) for t in DATA_TARGETS)]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        model_check = patch.object(ChatService, "_check_model", lambda self, user, model, capability, **kw: model)
        model_check.start()
        self.addCleanup(model_check.stop)
        self.service = ChatService(agent_factory=lambda *a, **k: None)
        service_patch = patch("src.admin.agents._make_service", lambda: self.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)

    def write_user(self, text: str | None = None) -> None:
        (self.config_dir / "user-a" / "user.yaml").write_text(
            text if text is not None else USER_YAML.format(hash=TEST_PASSWORD_HASH), encoding="utf-8",
        )

    def write_agent(self, name: str, text: str) -> None:
        (self.config_dir / "user-a" / "agents" / f"{name}.yaml").write_text(text, encoding="utf-8")

    def read(self, relative: str) -> str:
        return (self.config_dir / relative).read_text(encoding="utf-8")

    def run_cli(self, argv: list[str], stdin: str | None = None, passwords=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("getpass.getpass", side_effect=list(passwords)), \
                patch("sys.stdin", io.StringIO(stdin or "")):
            code = users.main(argv)
        return code, out.getvalue(), err.getvalue()
