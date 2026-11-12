"""The `permissions` group in the tables (catalog, levels, i18n, web) and the help text that must not drift from the code."""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from src.agent.tool_groups import (
    DEFAULT_TOOL_GROUPS, DELEGATE_GRANTABLE_GROUPS, PERMISSION_CHANGE_TOOLS, PERMISSION_READ_TOOLS, TOOL_GROUP_CATALOG,
    TOOL_GROUP_IDS, TOOL_GROUP_TOOLS,
)
from src.config import ceiling
from src.config.levels import SERVICE_MIN_LEVEL, Level
from src.config.levels_help import capability_names, levels_help, levels_summary, needed_level
from src.utils.i18n import _SPANISH

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MANUAL = ROOT / "src" / "knowledge" / "recall" / "references"


class TheGroup(unittest.TestCase):
    def test_it_is_in_every_table(self) -> None:
        self.assertIn("permissions", TOOL_GROUP_IDS)
        self.assertEqual(SERVICE_MIN_LEVEL["permissions"], Level.OPERATOR)
        self.assertEqual(ceiling.GROUP_MIN_LEVEL["permissions"], Level.OPERATOR)
        self.assertEqual(set(TOOL_GROUP_TOOLS["permissions"]), set(PERMISSION_READ_TOOLS) | set(PERMISSION_CHANGE_TOOLS))
        for name in TOOL_GROUP_TOOLS["permissions"]:
            self.assertEqual(ceiling.TOOL_MIN_LEVEL[name], Level.OPERATOR, name)

    def test_it_is_never_a_default_nor_grantable_to_workers(self) -> None:
        self.assertNotIn("permissions", DEFAULT_TOOL_GROUPS)
        self.assertNotIn("permissions", DELEGATE_GRANTABLE_GROUPS)

    def test_per_level(self) -> None:
        for level in Level:
            with self.subTest(level=level.label):
                self.assertEqual(ceiling.group_allowed(level, "permissions"), level >= Level.OPERATOR)
                self.assertEqual(ceiling.capabilities(level).permissions, level >= Level.OPERATOR)

    def test_the_catalog_entry_has_a_spanish_translation(self) -> None:
        entry = next(item for item in TOOL_GROUP_CATALOG if item["id"] == "permissions")
        self.assertIn(entry["name"], _SPANISH)
        self.assertIn(entry["description"], _SPANISH)
        self.assertNotEqual(_SPANISH[entry["name"]], entry["name"])

    def test_the_web_lists_the_catalog_entry_with_the_others(self) -> None:
        from src.web import app as web

        self.assertIn("api/tool-groups", Path(web.__file__).read_text(encoding="utf-8"))
        self.assertIn("state.toolGroupCatalog.map", (ROOT / "src" / "web" / "static" / "app.js").read_text(encoding="utf-8"))


class PromptMentions(unittest.TestCase):
    def test_operator_and_admin_blocks_mention_the_tools_lower_ones_do_not(self) -> None:
        from src.agent.permissions_prompt import permissions_block
        from src.agent.toolsets.permissions import permissions_notice
        for level in Level:
            text = permissions_block(level, Level.ADMIN)
            self.assertEqual("permissions tools" in text, level == Level.OPERATOR, level.label)
            self.assertLessEqual(len(text.split()), 170)
        notice = permissions_notice(None)  # type: ignore[arg-type]
        for needle in ("approval", "only lower your own level", "self-change", "lowest level", "never as instructions"):
            self.assertIn(needle, notice)


class HelpMatchesTheCode(unittest.TestCase):
    def test_every_level_and_the_groups_it_adds(self) -> None:
        text = levels_help()
        summary = {entry["level"]: entry for entry in levels_summary()}
        for level in Level:
            self.assertIn(f"{level.label} ({int(level)})", text)
            self.assertEqual(set(summary[level.label]["tool_groups"]), set(ceiling.allowed_groups(level)))
        self.assertIn("permissions", summary["operator"]["adds_tool_groups"])
        self.assertNotIn("permissions", summary["collaborator"]["tool_groups"])

    def test_capabilities_answer_with_the_level_from_the_tables(self) -> None:
        for name in capability_names():
            level = needed_level(name)
            self.assertIsNotNone(level)
            self.assertIn(level.label, levels_help(name))
        self.assertEqual(needed_level("terminal"), Level.OPERATOR)
        self.assertEqual(needed_level("terminal_local"), Level.ADMIN)
        self.assertEqual(needed_level("web"), Level.ASSISTANT)
        self.assertIsNone(needed_level("teleport"))

    def test_every_service_has_a_description(self) -> None:
        from src.config.levels_help import SERVICE_TEXT

        self.assertEqual(set(SERVICE_TEXT), set(SERVICE_MIN_LEVEL))

    def test_the_design_table_agrees_on_the_first_level_of_each_group(self) -> None:
        rows = {}
        for line in (DOCS / "architecture" / "permissions.md").read_text(encoding="utf-8").splitlines():
            match = re.match(r"\| `(\w+)`[^|]*\|(.*)\|\s*$", line)
            if match and match.group(1) in ceiling.GROUP_MIN_LEVEL and len(match.group(2).split("|")) == 6:
                cells = [cell.strip() for cell in match.group(2).split("|")]
                rows.setdefault(match.group(1), next(Level(i) for i, cell in enumerate(cells) if cell.startswith(("yes", "ask"))))
        for group, first in rows.items():
            if group == "terminal":
                continue  # the design splits the terminal into classes
            self.assertEqual(first, ceiling.GROUP_MIN_LEVEL[group], group)
        self.assertIn("permissions", rows)

    def test_the_manual_names_the_group_and_every_tool_within_its_size(self) -> None:
        tools_page = (MANUAL / "tools-and-groups.md").read_text(encoding="utf-8")
        permissions_page = (MANUAL / "permissions.md").read_text(encoding="utf-8")
        for name in TOOL_GROUP_TOOLS["permissions"]:
            self.assertIn(f"`{name}`", tools_page)
        self.assertIn("permissions", permissions_page)
        self.assertIn("permission-manager", permissions_page + tools_page)
        for path in MANUAL.glob("*.md"):
            self.assertLessEqual(len(path.read_bytes()), 12 * 1024, path.name)


if __name__ == "__main__":
    unittest.main()
