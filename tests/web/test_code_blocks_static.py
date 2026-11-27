"""The code block copy button and syntax colours: vendored library, shell precache, page wiring and translations."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from src.utils.i18n import _SPANISH

WEB = Path(__file__).resolve().parents[2] / "src" / "web"
STATIC = WEB / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


class CodeBlockAssetTests(unittest.TestCase):
    def test_highlight_is_vendored_with_its_license(self) -> None:
        library = (STATIC / "vendor" / "highlight.min.js").read_text(encoding="utf-8")
        self.assertIn("highlight.js v11", library.splitlines()[0])
        self.assertIn("window.hljs", library)
        self.assertIn("BSD 3-Clause", (STATIC / "vendor" / "highlight.LICENSE").read_text(encoding="utf-8"))

    def test_page_loads_highlight_before_the_app(self) -> None:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertLess(html.index("vendor/highlight.min.js"), html.index("/static/app.js"))

    def test_shell_precaches_highlight(self) -> None:
        shell = re.search(r"const SHELL = \[(.*?)\];", (WEB / "sw.js").read_text(encoding="utf-8"), re.S).group(1)
        self.assertIn("/static/vendor/highlight.min.js", shell)

    def test_every_vendored_script_in_the_shell_exists(self) -> None:
        shell = (WEB / "sw.js").read_text(encoding="utf-8")
        for name in re.findall(r'"/static/(vendor/[^"]+)"', shell):
            self.assertTrue((STATIC / name).is_file(), name)

    def test_button_strings_are_translated(self) -> None:
        for text in ("Copy", "Copy code", "Copied", "Failed to copy"):
            self.assertIn(text, _SPANISH)
            self.assertIn(f'"{text}"', APP_JS)

    def test_both_markdown_renderers_share_the_code_block_path(self) -> None:
        self.assertEqual(APP_JS.count("marked.parse("), 1)
        self.assertIn("renderMarkdown(target, text, true)", APP_JS)
        self.assertIn("renderMarkdown(body, text)", APP_JS)


if __name__ == "__main__":
    unittest.main()
