from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.agent.profile import AgentProfile, load_profile, open_items, system_prompt
from pydantic import ValidationError

from src.agent.dream import dream_folder_options
from src.agent.tools import build_tools, chat_folder_options
from src.agent.toolsets.self_files import MemoryOperationError, apply_memory_operations
from src.config.models import AgentConfig, ChatConfig, DreamConfig, FolderConfig, ModelsConfig, ResolvedAgent
from src.core.sources.registry import SourceRegistry
from src.utils.i18n import InputError

SKILL = "---\nname: weekly-plan\ndescription: Plan the user's week from the calendar\n---\n1. Read the calendar.\n2. Draft.\n"


def _config(**changes) -> ResolvedAgent:
    fields = dict(
        user_id="u", id="a", name="A", models=ModelsConfig(),
        provider_name="ollama", api_key="", embeddings_provider_name="ollama", embeddings_api_key="", level="admin",
    )
    return ResolvedAgent(**{**fields, **changes})


class AgentFilesFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=self.root / "data")
        self.files = self.sources.agent_files()
        self.agent_dir = self.root / "data" / "u" / "a"

    def tools(self, groups: list[str], config: ResolvedAgent | None = None, sources: SourceRegistry | None = None):
        host = SimpleNamespace(sources=sources or self.sources, config=config or _config(), index=None)
        return build_tools(host, None, groups)[1]


class SoulTests(AgentFilesFixture):
    def test_missing_soul_is_an_empty_personality_and_is_not_created(self) -> None:
        self.assertIsNone(self.files.soul())
        self.assertEqual(self.files.soul_text(), "")
        self.assertEqual(load_profile(self.sources).soul, "")
        self.assertFalse((self.agent_dir / "SOUL.md").exists())

    def test_history_keeps_the_last_five_versions_and_restores(self) -> None:
        self.files.write_soul("v0")
        for number in range(1, 8):
            self.assertTrue(self.files.write_soul(f"v{number}"))
        self.assertFalse(self.files.write_soul("v7"))  # unchanged: no snapshot
        history = self.files.soul_history()
        self.assertEqual(len(history), 5)
        self.assertEqual([self.files.soul_version(item["version"]) for item in history], ["v6", "v5", "v4", "v3", "v2"])
        oldest = history[-1]["version"]
        self.assertEqual(self.files.restore_soul(oldest), "v2")
        self.assertEqual(self.files.soul(), "v2")
        self.assertEqual(self.files.soul_version(self.files.soul_history()[0]["version"]), "v7")
        self.assertEqual(len(self.files.soul_history()), 5)

    def test_soul_limit_and_invalid_versions_are_rejected(self) -> None:
        with self.assertRaises(InputError):
            self.files.write_soul("x" * 20001)
        with self.assertRaises(InputError):
            self.files.soul_version("../SOUL")
        with self.assertRaises(FileNotFoundError):
            self.files.soul_version("20260101-000000-000000")

    def test_soul_edit_tool_patches_and_snapshots(self) -> None:
        self.files.write_soul("I am calm. I am brief.")
        soul_edit = self.tools(["self"])["soul_edit"]
        self.assertIn("error", soul_edit(action="patch", old_string="I am", new_string="x"))
        self.assertIn("updated", soul_edit(action="patch", old_string="I am brief.", new_string="I am concise."))
        self.assertEqual(self.files.soul(), "I am calm. I am concise.")
        self.assertEqual(self.files.soul_version(self.files.soul_history()[0]["version"]), "I am calm. I am brief.")

    def test_agent_source_only_exposes_its_own_entries(self) -> None:
        (self.agent_dir / "conversations").mkdir(exist_ok=True)
        (self.agent_dir / "conversations" / "c.jsonl").write_text("{}\n", encoding="utf-8")
        self.files.write_soul("soul")
        with self.assertRaises(ValueError):
            self.sources.read("agent:conversations/c.jsonl")
        with self.assertRaises(ValueError):
            self.sources.read("agent:../a/dream/review_state.json")
        self.assertEqual(self.sources.read("agent:SOUL.md"), "soul")
        self.assertEqual(self.files.list(), ["SOUL.md"])
        self.assertEqual(self.files.walk(), ["SOUL.md"])
        with self.assertRaises(PermissionError):
            self.sources.write("agent:SOUL.md", "x")
        self.assertNotIn("dream", self.sources.names())
        with self.assertRaises(PermissionError):
            self.sources.list("dream")


class MemoryToolTests(AgentFilesFixture):
    def memory(self, limit: int = 120):
        from unittest.mock import patch

        from src.config.models import MemoryLimits

        patcher = patch("src.config.settings.MEMORY_LIMITS", MemoryLimits(memory_chars=max(100, limit), user_chars=100))
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.tools(["self"], _config())["memory"]

    def test_add_patch_remove_follow_entry_semantics(self) -> None:
        entries = apply_memory_operations([], [{"action": "add", "content": "Likes tea"}])
        entries = apply_memory_operations(entries, [{"action": "add", "content": "Lives in Madrid"}])
        entries = apply_memory_operations(entries, [{"action": "patch", "old_text": "tea", "content": "green tea"}])
        self.assertEqual(entries, ["Likes green tea", "Lives in Madrid"])
        self.assertEqual(apply_memory_operations(entries, [{"action": "remove", "old_text": "Madrid"}]), ["Likes green tea"])
        for operation in (
            {"action": "add", "content": "Likes green tea"},  # duplicate
            {"action": "patch", "old_text": "Li", "content": "x"},  # ambiguous
            {"action": "remove", "old_text": "Paris"},  # missing
            {"action": "add", "content": "a\n§\nb"},  # separator
            {"action": "replace", "content": "x"},
        ):
            with self.subTest(operation=operation), self.assertRaises(MemoryOperationError):
                apply_memory_operations(entries, [operation])

    def test_tool_writes_entries_with_the_section_separator(self) -> None:
        memory = self.memory()
        self.assertIn("saved user", memory(target="user", action="add", content="Name: User A"))
        memory(target="user", action="add", content="Prefers Spanish")
        text = (self.agent_dir / "memories" / "USER.md").read_text(encoding="utf-8")
        self.assertEqual(text, "Name: User A\n§\nPrefers Spanish")
        self.assertEqual(self.files.memory_entries("user"), ["Name: User A", "Prefers Spanish"])

    def test_char_limit_is_checked_on_the_final_result_and_reports_entries(self) -> None:
        memory = self.memory(100)
        memory(target="memory", action="add", content="a" * 60)
        refused = memory(target="memory", action="add", content="b" * 60)
        self.assertIn("Nothing was saved", refused)
        self.assertIn("a" * 60, refused)
        self.assertEqual(self.files.memory_entries("memory"), ["a" * 60])
        # removing the old entry in the same batch makes room for the new one
        result = memory(target="memory", operations=[
            {"action": "remove", "old_text": "aaaa"}, {"action": "add", "content": "b" * 60},
        ])
        self.assertIn("saved memory: 1 entries, 60/100 chars", result)
        self.assertEqual(self.files.memory_entries("memory"), ["b" * 60])

    def test_batch_is_atomic(self) -> None:
        memory = self.memory()
        memory(target="memory", action="add", content="keep me")
        result = memory(target="memory", operations=[
            {"action": "add", "content": "new one"}, {"action": "remove", "old_text": "missing"},
        ])
        self.assertIn("operation 2", result)
        self.assertEqual(self.files.memory_entries("memory"), ["keep me"])
        self.assertIn("error", memory(target="other", action="add", content="x"))
        self.assertIn("error", memory(target="memory"))


class SkillToolTests(AgentFilesFixture):
    def test_create_view_patch_files_and_delete(self) -> None:
        tools = self.tools(["self"])
        manage, view = tools["skill_manage"], tools["skill_view"]
        self.assertIn("create done", manage(action="create", name="weekly-plan", content=SKILL))
        self.assertIn("already exists", manage(action="create", name="weekly-plan", content=SKILL))
        self.assertIn("patch done", manage(action="patch", name="weekly-plan", old_string="Draft.", new_string="Draft it."))
        self.assertIn("write_file done", manage(action="write_file", name="weekly-plan", file_path="references/tips.md", file_content="tip"))
        shown = view(name="weekly-plan")
        self.assertIn("Draft it.", shown)
        self.assertIn("references/tips.md", shown)
        self.assertEqual(view(name="weekly-plan", file_path="references/tips.md"), "tip")
        self.assertEqual([skill.name for skill in self.files.skills()], ["weekly-plan"])
        self.assertIn("error", manage(action="write_file", name="weekly-plan", file_path="../escape.md", file_content="x"))
        self.assertIn("delete done", manage(action="delete", name="weekly-plan"))
        self.assertEqual(self.files.skills(), [])

    def test_frontmatter_is_required_and_must_match_the_name(self) -> None:
        manage = self.tools(["self"])["skill_manage"]
        self.assertIn("error", manage(action="create", name="plan", content="no frontmatter"))
        self.assertIn("error", manage(action="create", name="plan", content=SKILL))  # name differs
        self.assertIn("error", manage(action="create", name="Bad Name", content=SKILL))

    def test_skill_view_is_available_without_the_self_group(self) -> None:
        host = SimpleNamespace(sources=self.sources, config=_config(), index=None)
        names = [spec["function"]["name"] for spec in build_tools(host, None, ["memory_read"], skill_view=True)[0]]
        self.assertIn("skill_view", names)
        self.assertNotIn("skill_manage", names)
        self.assertNotIn("memory", names)


class PromptAssemblyTests(AgentFilesFixture):
    def test_order_is_soul_memories_skills_for_user_then_extra_blocks(self) -> None:
        self.files.write_memory_text("memory", "Use metric units")
        self.files.write_memory_text("user", "Name: User A")
        self.files.write_skill("weekly-plan", SKILL)
        self.sources.mind_palace().write(
            "for_user.md", "# For the user\n- [x] answered one\n- first?\n  - why: curious\n- [ ] second?\n- third?\n- fourth?\n",
        )
        self.files.write_soul("I am the soul.")
        profile = load_profile(self.sources)
        self.assertEqual(profile.for_user, ["first?", "second?", "third?"])
        prompt = system_prompt(profile, ["## Connected services (MCP)\n\nnotes"])
        positions = [prompt.index(part) for part in (
            "I am the soul.", "Use metric units", "Name: User A", "weekly-plan: Plan the user's week",
            "Raise at most one", "- first?", "## Connected services",
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("answered one", prompt)
        self.assertNotIn("fourth?", prompt)

    def test_chat_prompt_is_frozen_until_a_new_chat_is_built(self) -> None:
        from unittest.mock import Mock, patch

        from src.agent.agent import Agent

        config = _config(tool_groups=["self"], associative_memory="off")
        provider = Mock()
        provider.context_window.return_value = None
        self.files.write_soul("I am the soul.")

        def build() -> Agent:
            with patch("src.agent.agent.VectorIndex.for_agent"):
                profile = load_profile(self.sources)
                return Agent(config, provider, sources=self.sources, profile=profile)

        first = build()
        self.assertIn("memory", first._tool_handlers)
        self.assertEqual(first._tool_handlers["memory"](target="user", action="add", content="Name: User A").split(":")[0], "saved user")
        self.assertNotIn("Name: User A", first.messages[0]["content"])
        first.open_chat(first.chat_id)
        self.assertNotIn("Name: User A", first.messages[0]["content"])
        second = build()
        self.assertTrue(second.messages[0]["content"].startswith("I am the soul."))
        self.assertIn("Name: User A", second.messages[0]["content"])

    def _prompt(self, groups: list[str], **changes) -> str:
        from unittest.mock import Mock, patch

        from src.agent.agent import Agent

        config = _config(tool_groups=groups, associative_memory="off", **changes)
        provider = Mock()
        provider.context_window.return_value = None
        with patch("src.agent.agent.VectorIndex.for_agent"):
            agent = Agent(config, provider, sources=self.sources, profile=load_profile(self.sources))
        return agent.messages[0]["content"]

    def test_memory_habits_block_reaches_agents_that_can_write_their_memory(self) -> None:
        prompt = self._prompt(["self"])
        self.assertIn("## Memory habits", prompt)
        self.assertIn("do not delete to make room", prompt)
        self.assertIn("The structure is yours", prompt)

    def test_memory_habits_block_is_left_out_without_memory_writes_or_memory(self) -> None:
        self.assertNotIn("## Memory habits", self._prompt(["memory_read"]))
        self.assertNotIn("## Memory habits", self._prompt(["self"], has_memory=False))

    def test_memory_habits_block_stays_short_and_names_no_agent_or_person(self) -> None:
        from src.agent.agent import _MEMORY_HABITS_NOTICE

        self.assertLessEqual(len(_MEMORY_HABITS_NOTICE), 1300)
        for word in ("Alice", "Bob", "Carol", "Atlas"):
            self.assertNotIn(word, _MEMORY_HABITS_NOTICE)

    def test_empty_profile_is_only_the_soul(self) -> None:
        self.assertEqual(system_prompt(AgentProfile(soul="Hi")), "Hi")
        self.assertEqual(open_items("no list here"), [])
        self.assertEqual(open_items("- [x] done\n- [ ] a\n- b\n", limit=20), ["a", "b"])


class FolderTests(AgentFilesFixture):
    def setUp(self) -> None:
        super().setUp()
        self.notes_dir = self.root / "notes"
        (self.notes_dir / "Projects").mkdir(parents=True)
        (self.notes_dir / ".obsidian").mkdir()
        (self.notes_dir / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        (self.notes_dir / "Projects" / "plan.md").write_text("# Plan\nship dream mode\n", encoding="utf-8")
        self.work_dir = self.root / "work"
        self.work_dir.mkdir()
        (self.work_dir / "todo.md").write_text("call the bank\n", encoding="utf-8")
        self.folders = [
            FolderConfig(name="notes", path=str(self.notes_dir)),
            FolderConfig(name="work", path=str(self.work_dir), read_only=True, indexed=False),
        ]

    def registry(self, groups: list[str], folders: list[FolderConfig] | None = None) -> SourceRegistry:
        options = chat_folder_options(_config(folders=self.folders if folders is None else folders, tool_groups=groups))
        return SourceRegistry.for_agent("u", "a", data_dir=self.root / "data", **options)

    def test_read_tools_hidden_entries_and_source_prefix(self) -> None:
        tools = self.tools(["folders_read"], sources=self.registry(["folders_read"]))
        self.assertNotIn("folder_write", tools)
        self.assertEqual(tools["folder_list"](), "notes: (read-only)\nwork: (read-only)")
        self.assertEqual(tools["folder_list"](path="notes:"), "notes:Projects/")
        self.assertIn("ship dream mode", tools["folder_read"](path="notes:Projects/plan.md"))
        self.assertIn("[notes:Projects/plan.md:2]", tools["folder_search"](query="DREAM"))
        self.assertIn("[work:todo.md:1]", tools["folder_search"](query="bank", path="work"))
        with self.assertRaises(ValueError):
            tools["folder_read"](path="notes:../data/u/a/SOUL.md")
        with self.assertRaises(ValueError):
            tools["folder_read"](path="notes:.obsidian/app.json")
        with self.assertRaises(ValueError):  # two folders: the prefix is required
            tools["folder_read"](path="Projects/plan.md")
        with self.assertRaises(PermissionError):
            self.registry(["folders_read"]).write("notes:x.md", "x")
        # the generic memory tools see the folders as sources too
        self.assertIn("notes", self.registry(["folders_read"]).names())

    def test_edit_tools_respect_read_only(self) -> None:
        sources = self.registry(["folders_edit"])
        tools = self.tools(["folders_edit"], sources=sources)
        self.assertEqual(tools["folder_list"](), "notes:\nwork: (read-only)")
        self.assertIn("edited", tools["folder_edit"](path="notes:Projects/plan.md", old_string="ship", new_string="shipped"))
        tools["folder_append"](path="notes:Projects/plan.md", content="done")
        tools["folder_move"](path="notes:Projects/plan.md", new_path="notes:Archive/plan.md")
        self.assertEqual((self.notes_dir / "Archive" / "plan.md").read_text(encoding="utf-8"), "# Plan\nshipped dream mode\ndone")
        self.assertIn("deleted", tools["folder_delete"](path="notes:Archive/plan.md"))
        # `read_only` wins over folders_edit, in the folder tools and in the generic edit tools
        self.assertEqual(tools["folder_write"](path="work:new.md", content="x"), "error: folder 'work' is read-only here")
        self.assertIn("read-only", tools["folder_move"](path="notes:Projects", new_path="work:x.md"))
        with self.assertRaises(PermissionError):
            sources.write("work:new.md", "x")
        self.assertFalse((self.work_dir / "new.md").exists())
        self.assertTrue(sources.get("notes").writable)
        self.assertFalse(sources.get("work").writable)

    def test_single_folder_accepts_paths_without_prefix(self) -> None:
        folders = [FolderConfig(name="notes", path=str(self.notes_dir))]
        tools = self.tools(["folders_read"], sources=self.registry(["folders_read"], folders))
        self.assertIn("ship dream mode", tools["folder_read"](path="Projects/plan.md"))

    def test_folders_without_tool_groups_are_indexed_but_not_readable(self) -> None:
        sources = self.registry(["memory_read"])
        self.assertNotIn("notes", sources.names())
        self.assertEqual([source.name for source in sources.indexed_sources() if source.name in ("notes", "work")], ["notes"])
        with self.assertRaises(PermissionError):
            sources.read("notes:Projects/plan.md")
        self.assertNotIn("folder_list", self.tools(["memory_read"], sources=sources))

    def test_missing_folder_is_skipped_when_loading(self) -> None:
        # an unmounted drive must not stop the agent; saving still rejects the path (see the API tests)
        with self.assertLogs("recall.sources", level="WARNING"):
            sources = self.registry(["folders_read"], [FolderConfig(name="gone", path=str(self.root / "missing"))])
        self.assertNotIn("gone", sources.names())

    def test_folder_validation(self) -> None:
        with self.assertRaises(ValidationError):
            FolderConfig(name="Bad Name", path=".")
        with self.assertRaises(ValidationError) as reserved:
            FolderConfig(name="mind_palace", path=".")
        self.assertIn("reserved", str(reserved.exception))
        with self.assertRaises(ValidationError) as duplicated:
            AgentConfig(id="a", name="A", folders=[{"name": "x", "path": "."}, {"name": "x", "path": ".."}])
        self.assertIn("unique", str(duplicated.exception))
        with self.assertRaises(ValidationError) as legacy:
            AgentConfig.model_validate({"id": "a", "name": "A", "vault": "C:/notes"})
        self.assertIn("folders", str(legacy.exception))

    def test_chat_and_dream_folder_options(self) -> None:
        self.assertEqual(chat_folder_options(_config(tool_groups=["folders_edit"])), {"recall_docs": True, "uploads": False})
        self.assertEqual(
            chat_folder_options(_config(folders=self.folders, tool_groups=["memory_read"])),
            {"recall_docs": True, "uploads": True, "folders": self.folders, "folders_readable": False, "folders_writable": False},
        )
        self.assertEqual(
            chat_folder_options(_config(folders=self.folders, tool_groups=["folders_read"])),
            {"recall_docs": True, "uploads": False, "folders": self.folders, "folders_readable": True, "folders_writable": False},
        )
        self.assertEqual(
            chat_folder_options(_config(folders=self.folders, tool_groups=["folders_edit"]))["folders_writable"], True,
        )
        self.assertFalse(chat_folder_options(_config(folders=self.folders, tool_groups=["folders_edit"], tools=False))["folders_readable"])
        dream = dream_folder_options(_config(folders=self.folders, dream=DreamConfig(tools=["folders"]), tool_groups=["folders_read"]))
        self.assertEqual(dream, {"recall_docs": True, "folders": self.folders, "folders_readable": True, "folders_writable": False})
        self.assertFalse(dream_folder_options(_config(folders=self.folders, dream=DreamConfig(tools=["web"])))["folders_readable"])

    def test_tool_group_renames(self) -> None:
        from src.agent.tool_groups import TOOL_GROUP_IDS, TOOL_GROUP_TOOLS

        self.assertTrue({"folders_read", "folders_edit"} <= TOOL_GROUP_IDS)
        self.assertFalse({"vault_read", "vault_edit"} & TOOL_GROUP_IDS)
        self.assertEqual(TOOL_GROUP_TOOLS["folders_read"], ("folder_list", "folder_read", "folder_search"))
        with self.assertRaises(ValidationError):
            ChatConfig(tool_groups=["vault_read"])


if __name__ == "__main__":
    unittest.main()
