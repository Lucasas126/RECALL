"""The in-app confirmation dialog: the markup, styles and translations exist and no native confirm is left."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.utils.i18n import messages

STATIC = Path(__file__).resolve().parents[2] / "src" / "web" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")


class ConfirmDialogStaticTests(unittest.TestCase):
    def test_no_native_dialogs_are_left(self) -> None:
        self.assertIsNone(re.search(r"(?<![\w.])(?:window\.)?(?:confirm|alert|prompt)\(", APP_JS))

    def test_markup_is_an_accessible_native_dialog(self) -> None:
        dialog = re.search(r'<dialog id="confirm-dialog"[^>]*>', INDEX_HTML).group(0)
        for needle in ('role="alertdialog"', "aria-labelledby", "aria-describedby"):
            self.assertIn(needle, dialog)
        for element_id in ("confirm-title", "confirm-message", "confirm-ok", "confirm-cancel"):
            self.assertEqual(len(re.findall(rf'\bid="{element_id}"', INDEX_HTML)), 1)

    def test_texts_are_set_with_textcontent_and_cancel_is_focused_for_danger(self) -> None:
        self.assertIn('$("confirm-message").textContent = spec.message', APP_JS)
        self.assertIn('(spec.danger ? $("confirm-cancel") : $("confirm-ok")).focus()', APP_JS)

    def test_styles_cover_danger_and_mobile(self) -> None:
        self.assertIn("#confirm-dialog::backdrop", STYLE_CSS)
        self.assertIn("#confirm-ok.danger", STYLE_CSS)
        self.assertEqual(STYLE_CSS.count("--on-danger:"), 3)  # dark, light, and light by system preference
        self.assertRegex(STYLE_CSS, r"@media \(max-width: 640px\) \{\s*#confirm-dialog")

    def test_every_label_is_translated(self) -> None:
        catalog = messages("es")
        for call in re.findall(r'(?:title|message|confirmLabel|cancelLabel): tr\("((?:[^"\\]|\\.)+)"', APP_JS):
            self.assertIn(call.replace('\\"', '"'), catalog, call)
        for label in ("Confirm", "Cancel", "Delete chat", 'Delete "{title}"? Its messages cannot be recovered.'):
            self.assertIn(label, catalog)


if __name__ == "__main__":
    unittest.main()
