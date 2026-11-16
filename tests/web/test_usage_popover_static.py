"""The Claude plan usage sits under the composer next to the debug usage and opens as a floating panel."""
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


class UsagePopoverTests(unittest.TestCase):
    def test_dots_sit_in_the_row_under_the_composer_beside_the_debug_usage(self) -> None:
        row = re.search(r'<div id="under-composer">(.*?)\n      </div>', HTML, re.S).group(1)
        self.assertLess(row.index('id="usage"'), row.index('id="claude-usage"'))
        self.assertIn('id="claude-usage-toggle"', row)
        self.assertIn('id="claude-usage-body"', row)
        self.assertLess(HTML.index('id="composer-row"'), HTML.index('id="under-composer"'))

    def test_panel_floats_above_the_dots_without_pushing_the_chat(self) -> None:
        self.assertRegex(CSS, r"#claude-usage-body \{ position: absolute; [^}]*bottom: calc\(100% \+ 6px\)")
        self.assertIn("#claude-usage-body[hidden] { display: none; }", CSS)

    def test_panel_closes_on_outside_click_escape_and_second_click(self) -> None:
        self.assertIn('addEventListener("pointerdown"', JS)
        self.assertIn('event.key === "Escape"', JS)
        self.assertIn('setClaudeUsageOpen($("claude-usage-body").hidden)', JS)

    def test_dots_button_keeps_one_line_height_on_touch_screens(self) -> None:
        # the touch rule gives every button min-height 44px; the dots must opt out and keep their tap area with borders
        self.assertRegex(CSS, r"#claude-usage-toggle \{ display: flex; min-height: 0; min-width: 0;")
        self.assertRegex(CSS, r"@media \(pointer: coarse\) \{ #claude-usage-toggle \{ min-height: 0;[^}]*margin: -6px 0;")

    def test_old_folded_state_is_gone(self) -> None:
        self.assertNotIn("usage-chevron", CSS + HTML)
        self.assertNotIn('localStorage.setItem("claude-usage-open"', JS)


if __name__ == "__main__":
    unittest.main()
