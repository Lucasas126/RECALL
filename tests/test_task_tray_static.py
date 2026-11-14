"""Running-subagents tray: the markup has the id the script uses, its texts are translated, the styles exist."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.utils.i18n import messages

STATIC = Path(__file__).resolve().parents[1] / "src" / "web" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")


class TaskTrayStaticTests(unittest.TestCase):
    def test_markup_script_and_styles(self) -> None:
        self.assertEqual(len(re.findall(r'\bid="tasks-tray"', INDEX_HTML)), 1)
        self.assertLess(INDEX_HTML.index('id="tasks-tray"'), INDEX_HTML.index('id="processes"'))
        self.assertIn('$("tasks-tray")', APP_JS)
        self.assertIn("#tasks-tray", STYLE_CSS)
        self.assertIn(".task-tray-row", STYLE_CSS)

    def test_the_chat_keeps_following_the_end_when_a_tray_opens(self) -> None:
        # opening or closing a tray above the composer resizes #messages: the view must stay at the end, not behind the tray
        self.assertRegex(APP_JS, r'new ResizeObserver\(\(\) => scrollDown\(\)\)\.observe\(\$\("messages"\)\)')

    def test_texts_are_translated(self) -> None:
        catalog = messages("es")
        for text in ("1 subagent running", "{count} subagents running"):
            self.assertIn(text, APP_JS.replace('\\"', '"'))
            self.assertIn(text, catalog)


if __name__ == "__main__":
    unittest.main()
