from __future__ import annotations

import argparse
import re
import tomllib
import typing
import unittest
from fnmatch import fnmatch
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.support.auth_helpers import TEST_PASSWORD_HASH, login
from src import knowledge
from src.admin import users as users_cli
from src.agent.profile import load_profile, profile_blocks
from src.agent.tool_groups import TOOL_GROUP_TOOLS
from src.agent.tools import build_tools
from src.agent.toolsets.self_files import build_self_tools
from src.config import models
from src.config.levels import Level
from src.core.sources.agent_files import BUILTIN_SKILL, AgentFilesSource
from src.core.sources.registry import SourceRegistry
from src.service.chat import ChatService
from src.utils.i18n import InputError, messages
from src.web.app import create_app

ROOT = Path(__file__).resolve().parents[2]
PACK = ROOT / "src" / "knowledge" / "recall"
HUB = PACK / "SKILL.md"
REFERENCES = sorted((PACK / "references").glob("*.md"))
TOOL_DEFINITION = re.compile(r"@tool\s+def (\w+)\(")
# backticked names that begin like a tool name but are not tools
NOT_TOOLS = {"query_model"}  # a settings.yaml key
TOOL_PREFIX = re.compile(r"^(folder|sandbox|dream|skill|soul|find|list|query|web)_[a-z_]+$")


def pack_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backticked(text: str) -> set[str]:
    return set(re.findall(r"`([^`\n]+)`", text))


def real_tools() -> set[str]:
    """Every tool defined with @tool in the toolsets (MCP tools are dynamic and not listed)."""
    found: set[str] = set()
    for path in (ROOT / "src" / "agent" / "toolsets").glob("*.py"):
        found.update(TOOL_DEFINITION.findall(path.read_text(encoding="utf-8")))
    return found


def fake_agent(root: Path, *, recall_knowledge: bool = True):
    sources = SourceRegistry.for_agent("u", "a", data_dir=root)
    return SimpleNamespace(sources=sources, config=SimpleNamespace(recall_knowledge=recall_knowledge, level=Level.ADMIN))


def tools_of(agent) -> dict:
    return {func.tool_spec["function"]["name"]: func for func in build_self_tools(agent)}


class PackStructureTests(unittest.TestCase):
    def test_hub_links_every_reference_and_every_link_exists(self) -> None:
        hub = pack_text(HUB)
        linked = set(re.findall(r"references/[\w-]+\.md", hub))
        for name in linked:
            with self.subTest(link=name):
                self.assertTrue((PACK / name).is_file())
        on_disk = {f"references/{path.name}" for path in REFERENCES}
        self.assertEqual(linked, on_disk, "the hub must list every reference file and only those")

    def test_frontmatter_and_version(self) -> None:
        text = knowledge.skill_text()
        meta = yaml.safe_load(text[3:text.find("\n---", 3)])
        self.assertEqual(meta["name"], "recall")
        self.assertLessEqual(len(meta["description"]), 140)
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(str(meta["version"]), version)
        self.assertNotIn("version:", pack_text(HUB), "the version is stamped at load time, not written in the file")

    def test_size_limits_and_line_endings_are_readable(self) -> None:
        self.assertLessEqual(len(HUB.read_bytes()), 6 * 1024)
        for path in REFERENCES:
            with self.subTest(file=path.name):
                self.assertLessEqual(len(path.read_bytes()), 12 * 1024)
        self.assertNotIn("\r", knowledge.skill_text())
        self.assertNotIn("\r", knowledge.read_file("references/architecture.md"))

    def test_cited_source_paths_exist(self) -> None:
        for path in [HUB, *REFERENCES]:
            for token in backticked(pack_text(path)):
                if not token.startswith(("src/", "tests/")) or any(mark in token for mark in "<>* "):
                    continue
                with self.subTest(file=path.name, path=token):
                    self.assertTrue((ROOT / token).exists())
        code_map = pack_text(PACK / "references" / "code-map.md")
        for token in backticked(code_map):
            if re.fullmatch(r"[\w./-]+\.(py|md|yaml|js|html|css)", token) and not token.startswith("."):
                with self.subTest(path=token):
                    self.assertTrue((ROOT / token).exists())

    def test_ships_in_the_docker_context(self) -> None:
        relative = "src/knowledge/recall/SKILL.md"
        parts = relative.split("/")
        for raw in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
            pattern = raw.strip()
            if not pattern or pattern.startswith(("#", "!")):
                continue
            pieces = pattern.rstrip("/").split("/")
            if pieces[0] == "**":
                pieces = pieces[1:]
                starts = range(len(parts))
            else:
                starts = range(1)
            for start in starts:
                window = parts[start:start + len(pieces)]
                if len(window) == len(pieces) and all(fnmatch(a, b) for a, b in zip(window, pieces)):
                    self.fail(f".dockerignore line {pattern!r} would drop the built-in knowledge skill")
        self.assertIn("COPY . .", (ROOT / "Dockerfile").read_text(encoding="utf-8"))


class ToolConsistencyTests(unittest.TestCase):
    def test_group_catalog_matches_defined_tools(self) -> None:
        grouped = {name for names in TOOL_GROUP_TOOLS.values() for name in names}
        self.assertTrue(grouped <= real_tools(), grouped - real_tools())

    def test_pack_names_only_real_tools_and_lists_every_one(self) -> None:
        tools = real_tools()
        for path in [HUB, *REFERENCES]:
            for token in backticked(pack_text(path)):
                if TOOL_PREFIX.match(token) and token not in NOT_TOOLS:
                    with self.subTest(file=path.name, token=token):
                        self.assertIn(token, tools)
        text = pack_text(PACK / "references" / "tools-and-groups.md")
        rows = set(re.findall(r"^\| `(\w+)` \|", text, flags=re.M))
        listed = rows & (tools | set(TOOL_GROUP_TOOLS))
        self.assertTrue(tools <= listed, f"tools missing from tools-and-groups.md: {sorted(tools - listed)}")
        for group, names in TOOL_GROUP_TOOLS.items():
            self.assertIn(f"`{group}`", text)
            for name in names:
                self.assertIn(f"`{name}`", text)


class ConfigConsistencyTests(unittest.TestCase):
    CLASSES = (
        models.AgentConfig, models.ChatConfig, models.CompactionConfig, models.DreamConfig, models.FolderConfig,
        models.AgentTerminalConfig, models.AgentTerminalBackend, models.AgentMcpConfig, models.AgentDelegationConfig,
        models.UserConfig, models.ProviderConfig, models.ModelsConfig, models.OpenRouterConfig,
        models.OllamaConfig, models.ClaudeSubscriptionConfig, models.SshBackendConfig, models.LocalBackendConfig,
    )

    def test_every_field_is_documented_and_every_documented_field_exists(self) -> None:
        text = pack_text(PACK / "references" / "agent-config.md")
        tokens = backticked(text)
        fields = {name for cls in self.CLASSES for name in cls.model_fields}
        for name in sorted(fields):
            with self.subTest(field=name):
                self.assertTrue(
                    name in tokens or any(token.endswith("." + name) for token in tokens),
                    f"add `{name}` to references/agent-config.md",
                )
        for name in re.findall(r"^\| `([\w.]+)` \|", text, flags=re.M):
            with self.subTest(documented=name):
                self.assertIn(name.rsplit(".", 1)[-1], fields)


def admin_commands() -> argparse.ArgumentParser:
    return users_cli._build_parser()


def resolve_command(tokens: list[str]) -> tuple[argparse.ArgumentParser, int]:
    """Walk the argparse tree; returns the deepest parser reached and how many tokens named sub-commands."""
    node, used = admin_commands(), 0
    for token in tokens:
        actions = [a for a in node._actions if isinstance(a, argparse._SubParsersAction)]
        if not actions or token not in actions[0].choices:
            break
        node, used = actions[0].choices[token], used + 1
    return node, used


def model_path_exists(cls, parts: list[str]) -> bool:
    """Whether dotted `parts` name a field of the pydantic model `cls` (below dict values, any key is accepted)."""
    index = 0
    while index < len(parts):
        if cls is None:
            return True
        fields = cls.model_fields
        if parts[index] not in fields:
            return bool(cls.model_config.get("extra") == "allow")
        annotation = fields[parts[index]].annotation
        index += 1
        if typing.get_origin(annotation) is dict:  # name -> model: the next segment is a key
            annotation = typing.get_args(annotation)[1]
            index += 1
        candidates = [annotation, *typing.get_args(annotation)]
        cls = next((a for a in candidates if isinstance(a, type) and issubclass(a, BaseModel)), None)
    return True


class AdminCliConsistencyTests(unittest.TestCase):
    PAGES = [HUB, *REFERENCES]

    def test_every_admin_command_named_in_the_pack_and_docs_exists(self) -> None:
        paths = [*self.PAGES, *sorted((ROOT / "docs").rglob("*.md")), ROOT / "src" / "agent" / "guide_soul.md"]
        seen = set()
        for path in paths:
            for match in re.finditer(r"python -m src\.admin((?: [a-z][a-z-]*)*)", pack_text(path)):
                tokens = match.group(1).split()
                if not tokens:
                    continue
                node, used = resolve_command(tokens)
                with self.subTest(file=path.name, command=" ".join(tokens)):
                    self.assertGreaterEqual(used, 1, "unknown admin command")
                    has_more = any(isinstance(a, argparse._SubParsersAction) for a in node._actions)
                    self.assertFalse(has_more, "incomplete command: a sub-command is required")
                seen.add(tokens[0])
        # the pack documents every top-level command
        top = {a for action in admin_commands()._actions if isinstance(action, argparse._SubParsersAction) for a in action.choices}
        text = pack_text(PACK / "references" / "admin-cli.md") + pack_text(PACK / "references" / "code-map.md")
        for command in sorted(top):
            with self.subTest(documented=command):
                self.assertIn(command, text)

    def test_every_option_used_in_examples_exists(self) -> None:
        for path in self.PAGES:
            for span in backticked(pack_text(path)):
                match = re.match(r"python -m src\.admin((?: [a-z][a-z-]*)+)", span)
                if not match:
                    continue
                node, _ = resolve_command(match.group(1).split())
                known = {opt for action in node._actions for opt in action.option_strings}
                for option in re.findall(r"(?<=\s)(--[a-z-]+)", span):
                    with self.subTest(file=path.name, span=span, option=option):
                        self.assertIn(option, known)

    def test_set_paths_in_examples_are_real_fields(self) -> None:
        for path in self.PAGES:
            for span in backticked(pack_text(path)):
                if "python -m src.admin" not in span or "--set" not in span:
                    continue
                cls = models.UserConfig if " user set" in span else models.AgentConfig
                for target in re.findall(r"--set ([A-Za-z0-9_.-]+)=", span):
                    if target == "path":  # the generic placeholder in the command table
                        continue
                    with self.subTest(file=path.name, path=target):
                        self.assertTrue(model_path_exists(cls, target.split(".")), target)

    def test_new_pages_fit_the_size_limits_and_are_linked(self) -> None:
        hub = pack_text(HUB)
        for name in ("admin-cli", "recipes", "security-for-admins", "delegation", "delegation-processes"):
            self.assertIn(f"references/{name}.md", hub)
            self.assertLessEqual(len((PACK / "references" / f"{name}.md").read_bytes()), 12 * 1024)

    def test_security_page_states_the_cross_user_and_secret_rules(self) -> None:
        text = pack_text(PACK / "references" / "security-for-admins.md")
        for needle in ("another user", "set-secret", "approval", "prompt", "/app/.config", "never"):
            self.assertIn(needle.lower(), text.lower())


class ReadOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.agent = fake_agent(self.root)
        self.files = self.agent.sources.agent_files()

    def test_index_lists_the_built_in_skill_first(self) -> None:
        self.files.write_skill("mine", "---\nname: mine\ndescription: my own procedure\n---\nsteps\n")
        skills = self.files.skills(include_builtin=True)
        self.assertEqual([skill.name for skill in skills], [BUILTIN_SKILL, "mine"])
        self.assertTrue(skills[0].builtin)
        self.assertFalse(skills[1].builtin)
        self.assertEqual([skill.name for skill in self.files.skills()], ["mine"])

    def test_view_hub_and_references(self) -> None:
        view = tools_of(self.agent)["skill_view"]
        self.assertIn("name: recall", view("recall"))
        self.assertIn("references/architecture.md", view("recall"))
        self.assertIn("Architecture", view("recall", "references/architecture.md"))

    def test_traversal_and_escape_are_refused(self) -> None:
        view = tools_of(self.agent)["skill_view"]
        for bad in ("../SKILL.md", "references/../../__init__.py", "/etc/passwd", "C:/Windows/win.ini", "..\\__init__.py", "references/../../../pyproject.toml"):
            with self.subTest(path=bad), self.assertRaises((InputError, FileNotFoundError)):
                view("recall", bad)
        with self.assertRaises(FileNotFoundError):
            view("recall", "references/missing.md")

    def test_symlink_escape_is_refused(self) -> None:
        outside = self.root / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        link = knowledge.KNOWLEDGE_DIR / "references" / "_link_test.md"
        try:
            try:
                link.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks are not available here")
            with self.assertRaises(InputError):
                knowledge.read_file("references/_link_test.md")
        finally:
            if link.is_symlink():
                link.unlink()

    def test_skill_manage_refuses_every_action_on_the_built_in_skill(self) -> None:
        manage = tools_of(self.agent)["skill_manage"]
        content = "---\nname: recall\ndescription: shadow\n---\nx\n"
        calls = (
            {"action": "create", "name": "recall", "content": content},
            {"action": "edit", "name": "recall", "content": content},
            {"action": "patch", "name": "recall", "old_string": "RECALL", "new_string": "X"},
            {"action": "delete", "name": "recall"},
            {"action": "write_file", "name": "recall", "file_path": "references/x.md", "file_content": "x"},
            {"action": "remove_file", "name": "recall", "file_path": "references/architecture.md"},
        )
        for call in calls:
            with self.subTest(action=call["action"]):
                result = manage(**call)
                self.assertTrue(result.startswith("error:"), result)
                self.assertIn("built-in", result)
                self.assertIn("different name", result)
        self.assertFalse((self.root / "u" / "a" / "skills" / "recall").exists())
        self.assertIn("name: recall", knowledge.skill_text())

    def test_own_skill_named_recall_does_not_shadow_it(self) -> None:
        with self.assertRaises(InputError):
            self.files.write_skill("recall", "---\nname: recall\ndescription: shadow\n---\nx\n")
        folder = self.root / "u" / "a" / "skills" / "recall"  # a folder left by hand
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text("---\nname: recall\ndescription: shadow\n---\nx\n", encoding="utf-8")
        skills = self.files.skills(include_builtin=True)
        self.assertEqual([skill.name for skill in skills], [BUILTIN_SKILL])
        self.assertNotIn("shadow", self.files.skill_text("recall"))

    def test_the_refusal_is_localized(self) -> None:
        template = next(key for key in messages("es") if key.startswith("'{name}' is a built-in skill"))
        self.assertNotEqual(messages("es")[template], template)

    def test_switch_off_removes_the_index_line_and_the_access(self) -> None:
        on = "\n".join(profile_blocks(load_profile(self.agent.sources, recall_knowledge=True)))
        off = "\n".join(profile_blocks(load_profile(self.agent.sources, recall_knowledge=False)))
        self.assertIn("Built in (read-only)", on)
        self.assertIn("- recall: How RECALL works", on)
        self.assertEqual(on.count("- recall:"), 1)
        self.assertNotIn("recall", off.lower().replace("recall mode", ""))
        off_agent = fake_agent(self.root, recall_knowledge=False)
        with self.assertRaises(FileNotFoundError):
            tools_of(off_agent)["skill_view"]("recall")
        self.assertIn("name: recall", tools_of(self.agent)["skill_view"]("recall"))

    def test_own_skills_keep_their_wording_without_the_built_in_one(self) -> None:
        self.files.write_skill("mine", "---\nname: mine\ndescription: my own procedure\n---\nsteps\n")
        block = "\n".join(profile_blocks(load_profile(self.agent.sources, recall_knowledge=False)))
        self.assertIn("Procedures you wrote for yourself", block)
        self.assertNotIn("Built in", block)

    def test_disposable_chats_can_read_but_not_manage(self) -> None:
        specs, handlers = build_tools(self.agent, None, ["memory_read"], skill_view=True, disposable=True)
        self.assertIn("skill_view", handlers)
        self.assertNotIn("skill_manage", handlers)
        self.assertIn("name: recall", handlers["skill_view"]("recall"))

    def test_dream_sessions_use_the_same_self_tools(self) -> None:
        source = (ROOT / "src" / "agent" / "dream.py").read_text(encoding="utf-8")
        self.assertIn("build_self_tools(host)", source)
        self.assertIn("recall_knowledge=self.config.recall_knowledge", source)
        host = fake_agent(self.root)  # the dream host exposes the same `sources` and `config`
        self.assertIn("name: recall", tools_of(host)["skill_view"]("recall"))


class SwitchConfigTests(unittest.TestCase):
    def test_default_is_on_and_round_trips(self) -> None:
        self.assertTrue(models.ChatConfig().recall_knowledge)
        config = models.AgentConfig(id="a", name="A", chat_config={"recall_knowledge": False})
        self.assertFalse(models.AgentConfig(**config.model_dump()).chat_config.recall_knowledge)
        self.assertIn("recall_knowledge", models.ResolvedAgent.model_fields)

    def test_form_and_documentation_carry_the_switch(self) -> None:
        script = (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        page = (ROOT / "src" / "web" / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('recall_knowledge: $("agent-recall-knowledge").checked', script)
        self.assertIn('id="agent-recall-knowledge"', page)
        self.assertIn("RECALL knowledge (how RECALL works)", messages("es"))
        self.assertIn("recall_knowledge", (ROOT / "docs" / "reference" / "agent-yaml.md").read_text(encoding="utf-8"))

    def test_web_api_stores_the_switch(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config_dir, data_dir = root / "config", root / "data"
        (config_dir / "user-a" / "agents").mkdir(parents=True)
        (config_dir / "user-a" / "user.yaml").write_text(
            f"id: user-a\nname: User A\nmax_level: admin\nprovider: {{name: ollama}}\nagents: []\npassword_hash: '{TEST_PASSWORD_HASH}'\n", encoding="utf-8",
        )
        for target in ("src.config.loader.CONFIG_DIR", "src.service.chat.CONFIG_DIR"):
            patcher = patch(target, config_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("src.config.loader.DATA_DIR", "src.service.chat.DATA_DIR", "src.core.sources.registry.DATA_DIR"):
            patcher = patch(target, data_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        service = ChatService(agent_factory=lambda *a, **k: None)
        client = TestClient(create_app(service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        login(client, "user-a")
        created = client.post("/api/agents", json={"id": "new", "name": "New"})
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["chat_config"]["recall_knowledge"])
        body = {"id": "new", "name": "New", "chat_config": {"recall_knowledge": False}}
        self.assertEqual(client.put("/api/agents/new", json=body).status_code, 200)
        self.assertFalse(client.get("/api/agents/new").json()["chat_config"]["recall_knowledge"])


if __name__ == "__main__":
    unittest.main()
