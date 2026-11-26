from __future__ import annotations

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent.parent / "src" / "web" / "static"


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
        found = re.search(r'const prompt = state\.busy\s*\?\s*"([^"]+)"', js)
        self.assertIsNotNone(found)
        self.assertLessEqual(len(found.group(1)), 24)

    def test_narrow_composer_has_one_options_button_and_no_second_row(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        button = re.search(r'<button id="composer-options"[^>]*>', html)
        self.assertIsNotNone(button)
        for part in ('aria-haspopup="true"', 'aria-expanded="false"', 'aria-controls="chat-controls"', "title=", "aria-label="):
            self.assertIn(part, button.group(0))
        for element_id in ("composer-options", "chat-controls", "model-picker", "chat-effort", "effort-policy", "model-policy"):
            self.assertEqual(html.count(f'id="{element_id}"'), 1, element_id)  # reused, never duplicated
        block = re.search(r"@container main \(max-width: 680px\) \{.*?\n\}", css, re.S)
        self.assertIsNotNone(block)
        text = block.group(0)
        self.assertIn('"attach opts input mic send"', text)
        self.assertNotIn('"controls controls', text)  # the second row is gone
        self.assertIn("#composer.options-open #chat-controls { display: flex; }", text)
        self.assertRegex(text, r"#model-policy:not\(\.privacy-warning\)\s*\{ display: block")
        self.assertIn("#composer.multiline { grid-template-columns: auto auto", text)  # multiline keeps one row
        self.assertIn("#composer-options { display: none;", css)  # wide layout unchanged
        self.assertIn('"Chat options"', (STATIC.parent.parent / "utils" / "i18n.py").read_text(encoding="utf-8"))
        self.assertIn('["composer-options", "Chat options"]', (STATIC / "app.js").read_text(encoding="utf-8"))

    def test_input_prompt_is_an_ellipsised_overlay(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('<span id="input-hint" aria-hidden="true">', html)
        self.assertRegex(html, r'<textarea id="input"[^>]*placeholder=" "[^>]*aria-label=')
        self.assertIn("pointer-events: none", re.search(r"#input-hint \{[^}]*\}", css).group(0))
        self.assertIn("white-space: nowrap", re.search(r"#input-hint \{[^}]*\}", css).group(0))
        self.assertIn("text-overflow: ellipsis", css)
        self.assertIn("@keyframes input-hint-marquee", css)
        self.assertRegex(css, r"prefers-reduced-motion: reduce\) \{ #input-hint\.scrolling #input-hint-text \{ animation: none")
        self.assertIn('$("input-hint-text").textContent = prompt;', js)
        self.assertNotIn('$("input").placeholder = state.busy', js)


if __name__ == "__main__":
    unittest.main()


class MakeDisposableNarrowTests(unittest.TestCase):
    def test_phones_show_only_the_icon_and_keep_the_name_for_assistive_technology(self) -> None:
        css = (STATIC / "style.css").read_text(encoding="utf-8")
        block = re.search(r"@media \(max-width: 600px\) \{\s*#chat-make-disposable \{[^}]*\}[^@]*?\n\}", css, re.S)
        self.assertIsNotNone(block, "the narrow-screen rule of the make-disposable button is missing")
        self.assertIn("min-width: 44px", block.group(0))
        self.assertIn("#chat-make-disposable-text", block.group(0))
        self.assertNotIn("display: none", block.group(0))  # hidden visually only, still in the accessibility tree
        js = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('$("chat-make-disposable").setAttribute("aria-label"', js)


class InputHintAlignmentTests(unittest.TestCase):
    def test_touch_input_and_hint_match_the_44px_buttons(self):
        css = (Path(__file__).resolve().parents[2] / "src/web/static/style.css").read_text(encoding="utf-8")
        hint = next(line for line in css.splitlines() if line.startswith("#input-hint {"))
        self.assertNotIn("align-self", hint)
        rule = next(line for line in css.splitlines() if "@media (pointer: coarse) { #input, #input-hint" in line)
        self.assertIn("min-height: 44px", rule)
        self.assertIn("padding-block: calc((44px - 1.5 * .9375rem) / 2)", rule)
