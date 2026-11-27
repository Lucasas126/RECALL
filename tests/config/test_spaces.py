"""Spaces: model validation, derived max_level, resolve_owner, id clashes, login refusal, Claude owner."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from src.config import spaces
from src.config.agents import AgentConflict, AgentStore
from src.config.levels import Level
from src.config.loader import load_user
from src.config.models import AgentConfig, UserConfig

SPACE = "space-0a1b2c3d"
OTHER = "space-ffffffff"


def user_yaml(user_id: str, level: str, agents: list[str]) -> str:
    return (f"id: {user_id}\nname: {user_id}\nmax_level: {level}\nprovider: {{name: openrouter, api_key: KEY_NAME}}\n"
            f"agents: [{', '.join(agents)}]\npassword_hash: x\n")


def space_yaml(space_id: str, members: list[str], billing: str, agents: list[str], extra: str = "") -> str:
    return (f"id: {space_id}\nname: Shared\nkind: space\nmembers: [{', '.join(members)}]\nbilling_user: {billing}\n"
            f"provider: {{name: openrouter, api_key: KEY_NAME}}\nagents: [{', '.join(agents)}]\n{extra}")


class SpaceFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / ".config"
        self.data = Path(tmp.name) / "data"
        patcher = patch("src.config.loader.CONFIG_DIR", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, owner: str, text: str, agents: tuple[str, ...] = ()) -> None:
        (self.config / owner / "agents").mkdir(parents=True, exist_ok=True)
        (self.config / owner / "user.yaml").write_text(text, encoding="utf-8")
        for agent in agents:
            (self.config / owner / "agents" / f"{agent}.yaml").write_text(f"id: {agent}\nname: {agent}\n", encoding="utf-8")


class SpaceModelTests(unittest.TestCase):
    def build(self, **overrides):
        data = {"id": SPACE, "name": "S", "kind": "space", "members": ["user-a", "user-b"], "billing_user": "user-a",
                "provider": {"name": "openrouter", "api_key": "K"}, "agents": []}
        data.update(overrides)
        return UserConfig.model_validate(data)

    def test_valid_space_has_placeholder_level(self) -> None:
        self.assertEqual(self.build().max_level, Level.MINIMAL)

    def test_declared_max_level_is_refused(self) -> None:
        with self.assertRaisesRegex(ValidationError, "must not declare max_level"):
            self.build(max_level="admin")

    def test_space_rules(self) -> None:
        for bad in ({"members": []}, {"billing_user": "user-c"}, {"billing_user": None}, {"id": "team"},
                    {"members": ["user-a", "user-a"]}, {"password_hash": "x"}, {"id": "space-ABCDEF12"}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                self.build(**bad)

    def test_user_cannot_carry_space_fields(self) -> None:
        base = {"id": "user-a", "name": "A", "max_level": "admin", "provider": {"name": "openrouter", "api_key": "K"},
                "agents": []}
        self.assertEqual(UserConfig.model_validate(base).kind, "user")
        for extra in ({"members": ["user-b"]}, {"billing_user": "user-b"}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                UserConfig.model_validate({**base, **extra})

    def test_claude_owner(self) -> None:
        self.assertEqual(self.build().claude_owner, "user-a")
        user = UserConfig.model_validate({"id": "user-b", "name": "B", "max_level": "admin",
                                          "provider": {"name": "openrouter", "api_key": "K"}, "agents": []})
        self.assertEqual(user.claude_owner, "user-b")


class SpaceResolutionTests(SpaceFixture):
    def setUp(self) -> None:
        super().setUp()
        self.put("user-a", user_yaml("user-a", "operator", ["own"]), ("own",))
        self.put("user-b", user_yaml("user-b", "assistant", ["mine"]), ("mine",))
        self.put("user-c", user_yaml("user-c", "admin", []))
        self.put(SPACE, space_yaml(SPACE, ["user-a", "user-b"], "user-a", ["shared"]), ("shared",))

    def test_max_level_is_the_lowest_of_members_on_every_load(self) -> None:
        self.assertEqual(load_user(SPACE).max_level, Level.ASSISTANT)
        self.put("user-b", user_yaml("user-b", "collaborator", ["mine"]))
        self.assertEqual(load_user(SPACE).max_level, Level.COLLABORATOR)
        self.assertEqual(AgentStore(self.config, self.data).user(SPACE).max_level, Level.COLLABORATOR)

    def test_missing_member_makes_the_space_unusable(self) -> None:
        (self.config / "user-b" / "user.yaml").unlink()
        with self.assertRaises(spaces.SpaceError):
            load_user(SPACE)

    def test_resolve_owner(self) -> None:
        self.assertEqual(spaces.resolve_owner("user-a", "own", self.config), "user-a")
        self.assertEqual(spaces.resolve_owner("user-a", "shared", self.config), SPACE)
        self.assertEqual(spaces.resolve_owner("user-b", "shared", self.config), SPACE)
        self.assertIsNone(spaces.resolve_owner("user-c", "shared", self.config))
        self.assertIsNone(spaces.resolve_owner("user-a", "nope", self.config))
        self.assertIsNone(spaces.resolve_owner("user-a", "mine", self.config))  # another user's private agent
        self.assertIsNone(spaces.resolve_owner(SPACE, "shared", self.config))  # a space is never a caller
        self.assertIsNone(spaces.resolve_owner("../x", "shared", self.config))

    def test_spaces_of(self) -> None:
        self.put(OTHER, space_yaml(OTHER, ["user-b", "user-c"], "user-c", []))
        self.assertEqual(spaces.spaces_of("user-a", self.config), [SPACE])
        self.assertEqual(spaces.spaces_of("user-b", self.config), [SPACE, OTHER])
        self.assertEqual(spaces.spaces_of("user-x", self.config), [])

    def test_members_conflict_checks_own_and_other_spaces(self) -> None:
        space = spaces._read(self.config, SPACE)
        self.assertIsNone(spaces.members_conflict(space, "user-c", self.config))
        self.put("user-c", user_yaml("user-c", "admin", ["shared"]), ("shared",))
        self.assertIn("clashes", spaces.members_conflict(space, "user-c", self.config))

    def test_agent_create_refuses_clashes(self) -> None:
        store = AgentStore(self.config, self.data)
        with self.assertRaises(AgentConflict):  # in the space: a member already has `own`
            store.create(SPACE, AgentConfig(id="own", name="x"))
        with self.assertRaises(AgentConflict):  # a member's own agent that the space already has
            store.create("user-a", AgentConfig(id="shared", name="x"))
        store.create(SPACE, AgentConfig(id="fresh", name="x"))
        self.assertIn("fresh", store.user(SPACE).agents)


class SpaceLoginTests(SpaceFixture):
    def test_login_and_authenticate_refuse_a_space(self) -> None:
        from src.service.chat import ChatService, UnknownUser

        self.put("user-a", user_yaml("user-a", "operator", []))
        self.put(SPACE, space_yaml(SPACE, ["user-a"], "user-a", []))
        service = ChatService(agent_factory=lambda *a, **k: None)
        with patch("src.service.chat.CONFIG_DIR", self.config), patch("src.config.settings.CONFIG_DIR", self.config):
            with self.assertRaises(UnknownUser):
                service.login(SPACE)
            with self.assertRaises(UnknownUser):
                service.authenticate(SPACE, "anything")


class SpaceClaudeLoginTests(SpaceFixture):
    def test_login_folder_resolves_in_the_billing_users_folder_only(self) -> None:
        from src.providers.claude_subscription.login import own_login_dir
        from src.utils.i18n import InputError

        self.put("user-a", user_yaml("user-a", "operator", []))
        self.put(SPACE, space_yaml(SPACE, ["user-a"], "user-a", [], "claude_subscription: {claude_config_dir: claude-login}\n"))
        space = load_user(SPACE)
        with patch("src.config.settings.CONFIG_DIR", self.config):
            folder = own_login_dir(space.claude_owner, space.claude_subscription)
            self.assertEqual(Path(folder), (self.config / "user-a" / "claude-login").resolve())
            with self.assertRaises(InputError):  # never an arbitrary path
                own_login_dir(space.claude_owner, load_user(SPACE).claude_subscription.model_copy(update={"claude_config_dir": "/tmp/x"}))


if __name__ == "__main__":
    unittest.main()
