from __future__ import annotations

import re
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.agent.permissions_prompt import block_for, permissions_block
from src.agent.profile import AgentProfile, system_prompt
from src.config.levels import Level

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "src" / "knowledge" / "recall" / "references" / "permissions.md"


class BlockTests(unittest.TestCase):
    def test_block_differs_per_level_and_names_it(self) -> None:
        blocks = {level: permissions_block(level, Level.ADMIN) for level in Level}
        self.assertEqual(len(set(blocks.values())), len(Level))
        for level, text in blocks.items():
            self.assertIn(f"{level.label} ({int(level)} of 5)", text)
            self.assertIn("Can", text)
            self.assertIn("Cannot", text)

    def test_contains_max_level_and_the_firm_instruction(self) -> None:
        text = permissions_block("reader", "operator")
        self.assertIn("maximum level: operator (4)", text)
        self.assertIn("enforced by the system", text)
        self.assertIn("do not use it", text)
        self.assertIn("which level", text)
        self.assertIn("administrator", text)
        self.assertIn("references/permissions.md", text)

    def test_compact_and_stable(self) -> None:
        for level in Level:
            for dream in (False, True):
                text = permissions_block(level, Level.ADMIN, dream=dream)
                self.assertLessEqual(len(text.split()), 180)
                self.assertEqual(text, permissions_block(level, Level.ADMIN, dream=dream))
                self.assertFalse(re.search(r"\d{4}-\d{2}-\d{2}", text))

    def test_dream_variant_says_unattended(self) -> None:
        self.assertIn("unattended dream session", permissions_block("assistant", "admin", dream=True))
        self.assertNotIn("dream session", permissions_block("assistant", "admin"))

    def test_no_level_means_no_block(self) -> None:
        self.assertEqual(permissions_block(None, "admin"), "")
        self.assertEqual(permissions_block("nonsense"), "")
        self.assertEqual(block_for(SimpleNamespace()), "")
        self.assertEqual(block_for(SimpleNamespace(level=None, max_level=None), dream=True), "")

    def test_missing_max_level_still_gives_a_block(self) -> None:
        text = block_for(SimpleNamespace(level=Level.ASSISTANT, max_level=None))
        self.assertIn("assistant", text)
        self.assertNotIn("owner's maximum", text)


class PromptAssemblyTests(unittest.TestCase):
    def test_block_sits_after_soul_and_before_other_blocks(self) -> None:
        profile = AgentProfile(soul="I am SOUL", memory=["note"])
        block = permissions_block("collaborator", "admin")
        prompt = system_prompt(profile, ["extra"], block)
        self.assertLess(prompt.index("I am SOUL"), prompt.index("## Permissions"))
        self.assertLess(prompt.index("## Permissions"), prompt.index("## Your memory"))
        self.assertLess(prompt.index("## Your memory"), prompt.index("extra"))

    def test_without_block_prompt_is_unchanged(self) -> None:
        profile = AgentProfile(soul="I am SOUL")
        self.assertEqual(system_prompt(profile, ["extra"]), system_prompt(profile, ["extra"], ""))
        self.assertNotIn("Permissions", system_prompt(profile))

    def test_agent_prompt_uses_the_config_level(self) -> None:
        from src.agent.agent import Agent

        agent = Agent.__new__(Agent)
        agent.profile = AgentProfile(soul="S")
        agent._associative_parallel = lambda: False
        agent._disposable, agent._task = False, None
        agent._tool_handlers, agent._mcp_notes = {}, []
        agent.config = SimpleNamespace(level=Level.READER, max_level=Level.OPERATOR)
        prompt = agent._system_prompt()
        self.assertIn("reader (1 of 5)", prompt)
        self.assertIn("operator (4)", prompt)
        self.assertEqual(prompt, agent._system_prompt())
        agent.config = SimpleNamespace()  # built by hand: no level, no block, no crash
        self.assertNotIn("Permissions", agent._system_prompt())

    def test_worker_prompt_keeps_the_block(self) -> None:
        from src.agent.agent import Agent

        agent = Agent.__new__(Agent)
        agent.profile = AgentProfile(soul="worker instructions")
        agent._associative_parallel = lambda: False
        agent._disposable, agent._task = True, SimpleNamespace(kind="worker")
        agent._tool_handlers, agent._mcp_notes = {}, []
        agent.config = SimpleNamespace(level=Level.COLLABORATOR, max_level=Level.COLLABORATOR)
        prompt = agent._system_prompt()
        self.assertIn("collaborator (3 of 5)", prompt)
        self.assertNotIn("unattended dream", prompt)

    def test_dream_prompt_has_the_dream_block(self) -> None:
        from src.agent.dream import DreamRunner

        runner = DreamRunner.__new__(DreamRunner)
        runner.config = SimpleNamespace(
            level=Level.ASSISTANT, max_level=Level.ADMIN, dream=SimpleNamespace(prompt="dream now"),
        )
        prompt = runner._system_prompt(AgentProfile(soul="S"))
        self.assertIn("assistant (2 of 5)", prompt)
        self.assertIn("unattended dream session", prompt)
        self.assertIn("dream now", prompt)
        runner.config = SimpleNamespace(level=None, dream=SimpleNamespace(prompt="dream now"))
        self.assertNotIn("Permissions", runner._system_prompt(AgentProfile(soul="S")))


class SkillPageTests(unittest.TestCase):
    def test_page_exists_is_indexed_and_small(self) -> None:
        self.assertTrue(PAGE.is_file())
        self.assertLessEqual(len(PAGE.read_bytes()), 8 * 1024)
        hub = (ROOT / "src" / "knowledge" / "recall" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("references/permissions.md", hub)

    def test_page_covers_every_level_and_the_concepts(self) -> None:
        text = PAGE.read_text(encoding="utf-8")
        for level in Level:
            self.assertIn(f"`{level.label}`", text)
        for needle in ("dream", "manual", "smart", "off", "permissions", "max_level", "administrator", "ask"):
            self.assertIn(needle, text)
        self.assertEqual(text, text.replace("\r", ""))


if __name__ == "__main__":
    unittest.main()
