from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "web" / "static"


class MobileComposerTests(unittest.TestCase):
    def test_phones_show_the_model_and_the_effort_as_icons(self) -> None:
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        block = re.search(r"@media \(max-width: 520px\) \{\s*#chat-controls \{ flex-wrap: nowrap.*?\n\}", css, re.S)
        self.assertIsNotNone(block, "the phone rule that turns the two selectors into icons is missing")
        text = block.group(0)
        for selector in ("#model-picker", "#chat-effort"):
            self.assertIn(selector, text)
        self.assertIn("font-size: 0", text)  # the text is hidden, the icon is a mask over the control
        self.assertIn("#model-picker::before", text)
        self.assertIn("#chat-effort-label::after", text)

    def test_the_steer_placeholder_fits_one_line(self) -> None:
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        found = re.search(r'\$\("input"\)\.placeholder = state\.busy\s*\?\s*"([^"]+)"', js)
        self.assertIsNotNone(found)
        self.assertLessEqual(len(found.group(1)), 24)


if __name__ == "__main__":
    unittest.main()
