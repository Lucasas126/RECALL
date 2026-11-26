from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.profile import profile_blocks
from src.agent.toolsets.self_files import MemoryOperationError, build_self_tools
from src.agent.user_sections import apply_user_operations, parse_sections, user_limit
from src.config.models import MemoryLimits
from src.config.spaces import scaled_user_chars

MEMBERS = {"user-a": "Ana", "user-b": "Ben"}


def _agent(sources, members=None, speaker=None):
    config = SimpleNamespace(space_members=members or {}, space_billing_user="user-a" if members else None)
    return SimpleNamespace(sources=sources, config=config, speaker=speaker, _ledger=None)


class LimitScalingTests(unittest.TestCase):
    def test_scales_with_members_and_caps_at_five(self) -> None:
        self.assertEqual([scaled_user_chars(100, n) for n in (1, 2, 5, 7)], [100, 200, 500, 500])

    def test_private_agent_keeps_the_plain_limit(self) -> None:
        self.assertEqual(user_limit(SimpleNamespace(space_members={})), MemoryLimits().user_chars)

    def test_space_agent_limit(self) -> None:
        for count, factor in ((1, 1), (2, 2), (5, 5), (7, 5)):
            members = {f"user-{i}": f"N{i}" for i in range(count)}
            self.assertEqual(user_limit(SimpleNamespace(space_members=members)), MemoryLimits().user_chars * factor)


class SectionOperationTests(unittest.TestCase):
    def test_speaker_edits_only_their_section(self) -> None:
        text = "## user-a\nlikes tea\n\n## user-b\nlikes jazz\n"
        new, _ = apply_user_operations(text, [{"action": "add", "content": "runs"}], list(MEMBERS), "user-a")
        self.assertEqual(parse_sections(new), {"user-a": ["likes tea", "runs"], "user-b": ["likes jazz"]})
        new, _ = apply_user_operations(new, [{"action": "patch", "old_text": "tea", "content": "coffee"}], list(MEMBERS), "user-a")
        self.assertEqual(parse_sections(new)["user-a"], ["likes coffee", "runs"])
        with self.assertRaises(MemoryOperationError):  # user-b's entry is out of reach
            apply_user_operations(new, [{"action": "remove", "old_text": "jazz"}], list(MEMBERS), "user-a")
        new, _ = apply_user_operations(new, [{"action": "remove", "old_text": "runs"}], list(MEMBERS), "user-a")
        self.assertEqual(parse_sections(new), {"user-a": ["likes coffee"], "user-b": ["likes jazz"]})

    def test_first_add_creates_the_section_and_empty_headings_stay(self) -> None:
        new, _ = apply_user_operations("", [{"action": "add", "content": "x"}], list(MEMBERS), "user-b")
        self.assertEqual(parse_sections(new), {"user-b": ["x"]})
        new, _ = apply_user_operations(new, [{"action": "remove", "old_text": "x"}], list(MEMBERS), "user-b")
        self.assertIn("## user-b", new)

    def test_non_member_is_refused(self) -> None:
        with self.assertRaises(MemoryOperationError):
            apply_user_operations("", [{"action": "add", "content": "x"}], list(MEMBERS), "user-z")

    def test_no_speaker_edits_any_section_and_keeps_headings(self) -> None:
        text = "## user-a\nlikes tea\n\n## user-b\nlikes jazz\n"
        new, _ = apply_user_operations(text, [{"action": "remove", "old_text": "jazz"}], list(MEMBERS), None)
        self.assertEqual(parse_sections(new), {"user-a": ["likes tea"], "user-b": []})
        new, _ = apply_user_operations(new, [{"action": "add", "section": "user-b", "content": "opera"}], list(MEMBERS), None)
        self.assertEqual(parse_sections(new)["user-b"], ["opera"])
        with self.assertRaises(MemoryOperationError):
            apply_user_operations(new, [{"action": "add", "content": "no section"}], list(MEMBERS), None)


class ToolAndPromptTests(unittest.TestCase):
    def test_tool_and_prompt_for_a_space_agent(self) -> None:
        with TemporaryDirectory() as root, patch("src.config.settings.MEMORY_LIMITS", MemoryLimits(user_chars=100)):
            files = SimpleNamespace(
                text="", memory_lock=lambda: __import__("contextlib").nullcontext(),
            )
            files.memory_text = lambda target: files.text
            files.memory_entries = lambda target: []
            files.write_memory_text = lambda target, text: setattr(files, "text", text)
            sources = SimpleNamespace(agent_files=lambda: files)
            agent = _agent(sources, MEMBERS, speaker="user-b")
            memory = {t.tool_spec["function"]["name"]: t for t in build_self_tools(agent)}["memory"]
            self.assertIn("saved user", memory(target="user", action="add", content="likes jazz"))
            self.assertEqual(parse_sections(files.text), {"user-b": ["likes jazz"]})
            agent.speaker = None  # no speaker known: the billing member
            memory(target="user", action="add", content="likes tea")
            self.assertEqual(parse_sections(files.text)["user-a"], ["likes tea"])
            self.assertIn("error", memory(target="user", action="add", content="x" * 190))  # 2 members = 200 chars
            self.assertIn("error", memory(target="user", action="remove", old_text="jazz"))  # user-b's entry
            self.assertEqual(parse_sections(files.text)["user-b"], ["likes jazz"])

    def test_prompt_header_shows_the_scaled_limit_and_names(self) -> None:
        with TemporaryDirectory() as root:
            from src.agent.profile import AgentProfile

            text = "## user-a\nlikes tea\n\n## user-b\nlikes jazz\n"
            profile = AgentProfile(user_text=text, user_names=MEMBERS, user_chars=5000)
            block = next(b for b in profile_blocks(profile) if "About the users" in b)
            self.assertIn(f"/{5000} chars", block)
            self.assertIn("## Ana (user-a)\nlikes tea", block)
            self.assertIn("## Ben (user-b)\nlikes jazz", block)

    def test_private_profile_is_unchanged(self) -> None:
        from src.agent.profile import AgentProfile

        profile = AgentProfile(user=["likes tea"])
        block = next(b for b in profile_blocks(profile) if "About the user" in b)
        self.assertTrue(block.startswith(f"## About the user (9/{MemoryLimits().user_chars} chars)"))


if __name__ == "__main__":
    unittest.main()
