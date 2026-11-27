"""@mentions, delegation targets and delegate resolution never cross owners (person <-> space, space <-> space)."""

from __future__ import annotations

from types import SimpleNamespace

from src.utils.i18n import InputError
from src.config.models import AgentConfig
from src.service.tasks import TaskError, TaskManager
from tests.support.admin_helpers import AdminFixture

SPACE = "space-0a1b2c3d"
OTHER = "space-ffffffff"


class OwnerIsolationTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_user(
            "id: user-a\nname: A\nmax_level: admin\nprovider: {name: ollama, api_key: K}\nagents: [bot, nova]\n"
            "password_hash: x\n"
        )
        self.write_agent("nova", "id: nova\nname: Nova\n")
        for space, agents in ((SPACE, ["sage", "echo"]), (OTHER, ["rex"])):
            (self.config_dir / space / "agents").mkdir(parents=True)
            (self.config_dir / space / "user.yaml").write_text(
                f"id: {space}\nname: S\nkind: space\nmembers: [user-a]\nbilling_user: user-a\n"
                f"provider: {{name: ollama, api_key: K}}\nagents: [{', '.join(agents)}]\n", encoding="utf-8")
            for agent in agents:
                (self.config_dir / space / "agents" / f"{agent}.yaml").write_text(f"id: {agent}\nname: {agent}\n", encoding="utf-8")
        self.manager = TaskManager(self.service, self.data_dir)

    def mention(self, owner: str, agent: str, target: str):
        return self.service._mention_target(owner, agent, f"@{target} hello")

    def test_mention_same_owner_allowed(self) -> None:
        self.assertEqual(self.mention("user-a", "bot", "nova"), ("nova", "hello"))
        self.assertEqual(self.mention(SPACE, "sage", "echo"), ("echo", "hello"))

    def test_mention_never_crosses_owners(self) -> None:
        self.assertIsNone(self.mention("user-a", "bot", "sage"))  # private -> space
        self.assertIsNone(self.mention(SPACE, "sage", "bot"))  # space -> private
        self.assertIsNone(self.mention(SPACE, "sage", "rex"))  # space -> other space

    def target(self, owner: str, agent: str, target: str) -> dict:
        parent = SimpleNamespace(user_id=owner, agent_id=agent)
        return self.manager._resolve_target(parent, target, [{"agent": target, "remember": False}])

    def test_delegate_same_owner_allowed(self) -> None:
        self.assertEqual(self.target("user-a", "bot", "nova")["id"], "nova")
        self.assertEqual(self.target(SPACE, "sage", "echo")["id"], "echo")

    def test_delegate_never_crosses_owners(self) -> None:
        for owner, agent, target in (("user-a", "bot", "sage"), (SPACE, "sage", "bot"), (SPACE, "sage", "rex")):
            with self.subTest(owner=owner, target=target), self.assertRaises(TaskError):
                self.target(owner, agent, target)

    def check(self, owner: str, targets: list[str]) -> None:
        config = AgentConfig.model_validate({"id": "x", "name": "X", "delegation": {"targets": [{"agent": t} for t in targets]}})
        self.service._check_delegation_targets(self.service._user(owner), config)

    def test_settings_validation_same_owner_only(self) -> None:
        self.check("user-a", ["nova"])
        self.check(SPACE, ["echo"])
        for owner, target in (("user-a", "sage"), (SPACE, "bot"), (SPACE, "rex")):
            with self.subTest(owner=owner, target=target), self.assertRaises(InputError):
                self.check(owner, [target])
