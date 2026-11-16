"""`search` paging and path filter, `skill_view` folder listing, and the profile's loaded line."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


from src.agent.profile import AgentProfile, load_profile, profile_blocks, system_prompt
from tests.agent.test_agent_files import AgentFilesFixture, SKILL


class SearchTests(AgentFilesFixture):
    def setUp(self) -> None:
        super().setUp()
        palace = self.agent_dir / "mind_palace"
        for name in ("a", "b"):
            (palace / "notes").mkdir(parents=True, exist_ok=True)
            (palace / "notes" / f"{name}.md").write_text("\n".join(f"needle {name} {i}" for i in range(30)), encoding="utf-8")
        (palace / "other.md").write_text("needle other\n", encoding="utf-8")
        (palace / "deep" / "x").mkdir(parents=True)
        (palace / "deep" / "x" / "n.txt").write_text("needle deep\n", encoding="utf-8")
        self.search = self.tools(["memory_read"])["search"]

    def test_default_reports_total_and_next_page(self) -> None:
        out = self.search("needle")
        self.assertEqual(out.count("[mind_palace:"), 50)
        self.assertIn("showing 1-50 of 62 matches; next page: offset=50", out)

    def test_paging_and_limit(self) -> None:
        out = self.search("needle", offset=50, limit=200)
        self.assertEqual(out.count("[mind_palace:"), 12)
        self.assertIn("showing 51-62 of 62 matches", out)
        self.assertNotIn("next page", out)
        self.assertEqual(self.search("needle", limit=5000).count("[mind_palace:"), 62)  # clamped to 200
        self.assertIn("62 in total", self.search("needle", offset=500))

    def test_path_prefix_and_glob(self) -> None:
        self.assertIn("of 60 matches", self.search("needle", path="mind_palace/notes/"))
        self.assertIn("of 60 matches", self.search("needle", path="notes/"))
        only = self.search("needle", path="mind_palace/**/*.md")
        self.assertIn("of 61 matches", only)
        self.assertNotIn("n.txt", only)
        self.assertIn("a.md", self.search("needle", path="mind_palace/notes/a.*", limit=1))
        self.assertEqual(self.search("needle", path="nowhere/"), "no matches.")

    def test_path_filter_reaches_skills(self) -> None:
        self.files.write_skill("weekly-plan", SKILL)
        self.files.write_skill_file("weekly-plan", "references/tips.md", "needle tip\n")
        out = self.search("needle", path="skills/weekly-plan/")
        self.assertIn("agent:skills/weekly-plan/references/tips.md:1", out)
        self.assertIn("of 1 matches", out)


class SkillFolderTests(AgentFilesFixture):
    def test_own_skill_lists_nested_files_and_folders(self) -> None:
        self.files.write_skill("weekly-plan", SKILL)
        for path in ("references/tips.md", "references/deep/more.md", "templates/t.txt", "scripts/run.sh"):
            self.files.write_skill_file("weekly-plan", path, "x")
        view = self.tools(["self"])["skill_view"]
        shown = view("weekly-plan")
        for path in ("references/tips.md", "references/deep/more.md", "templates/t.txt", "scripts/run.sh"):
            self.assertIn(path, shown)
        folder = view("weekly-plan", "references/")
        self.assertIn("references/tips.md", folder)
        self.assertIn("references/deep/more.md", folder)
        self.assertNotIn("templates/t.txt", folder)
        self.assertEqual(view("weekly-plan", "references"), folder)
        self.assertEqual(view("weekly-plan", "references/tips.md"), "x")
        with self.assertRaises(FileNotFoundError):
            view("weekly-plan", "nothing/")

    def test_builtin_skill_lists_references(self) -> None:
        view = self.tools(["self"])["skill_view"]
        self.assertIn("references/architecture.md", view("recall"))
        self.assertIn("references/terminal-and-approvals.md", view("recall", "references/"))


class LoadedLineTests(AgentFilesFixture):
    def test_loaded_line_is_the_last_profile_block(self) -> None:
        self.files.write_soul("I am calm.")
        self.files.write_skill("weekly-plan", SKILL)
        blocks = profile_blocks(load_profile(self.sources))
        self.assertRegex(
            blocks[-1], r"^Profile loaded \d{4}-\d\d-\d\dT[\d:]+[+-Z][\d:]*; SOUL 10 chars, 1 skills \(own\) \+ built-in recall$",
        )
        self.assertTrue(system_prompt(load_profile(self.sources)).rstrip().rsplit("\n", 1)[-1].startswith("Profile loaded "))
        off = profile_blocks(load_profile(self.sources, recall_knowledge=False))
        self.assertTrue(off[-1].endswith("1 skills (own)"))
        self.assertNotIn("/", off[-1])  # no paths

    def test_hand_built_profile_has_no_line(self) -> None:
        self.assertEqual(profile_blocks(AgentProfile()), [])


if __name__ == "__main__":
    unittest.main()
