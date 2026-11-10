"""Profile pictures UI: the markup has the ids the script uses, every new text is translated, the styles exist."""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from src.utils.i18n import messages

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "web" / "static"
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "style.css").read_text(encoding="utf-8")

AGENT_IDS = (
    "agent-avatar-section", "agent-avatar-legend", "agent-avatar-preview", "agent-avatar-upload", "agent-avatar-remove",
    "agent-avatar-file", "agent-avatar-hint", "agent-avatar-free", "agent-avatar-free-label", "agent-avatar-free-hint",
    "agent-avatar-error",
)
SETTINGS_IDS = (
    "settings-avatar", "settings-avatar-title", "settings-avatar-note", "settings-avatar-preview", "settings-avatar-upload",
    "settings-avatar-remove", "settings-avatar-file", "settings-avatar-hint", "settings-avatar-free",
    "settings-avatar-free-label", "settings-avatar-free-hint", "settings-avatar-error",
)


class AvatarMarkupTests(unittest.TestCase):
    def test_new_ids_exist_once_and_the_script_uses_them(self) -> None:
        for dom_id in (*AGENT_IDS, *SETTINGS_IDS):
            with self.subTest(id=dom_id):
                self.assertEqual(len(re.findall(rf'\bid="{dom_id}"', INDEX_HTML)), 1)
        for dom_id in ("agent-avatar-preview", "agent-avatar-upload", "agent-avatar-remove", "agent-avatar-file",
                       "agent-avatar-free", "agent-avatar-error", "settings-avatar-preview", "settings-avatar-upload",
                       "settings-avatar-remove", "settings-avatar-file", "settings-avatar-free", "settings-avatar-error"):
            with self.subTest(script=dom_id):
                self.assertIn(f'$("{dom_id}")', APP_JS)

    def test_the_existing_avatar_slots_are_kept(self) -> None:
        self.assertIn('id="user-avatar"', INDEX_HTML)
        self.assertIn('id="agent-avatar"', INDEX_HTML)

    def test_file_inputs_accept_only_png_jpeg_and_webp(self) -> None:
        for dom_id in ("agent-avatar-file", "settings-avatar-file"):
            tag = re.search(rf'<input id="{dom_id}"[^>]*>', INDEX_HTML).group(0)
            self.assertIn('type="file"', tag)
            self.assertIn('accept="image/png,image/jpeg,image/webp"', tag)
            self.assertIn("hidden", tag)

    def test_the_agent_block_is_in_the_basic_panel_and_the_settings_block_in_the_form(self) -> None:
        basic = INDEX_HTML.index('id="agent-panel-basic"')
        block = INDEX_HTML.index('id="agent-avatar-section"')
        next_panel = INDEX_HTML.index('id="agent-panel-', basic + 10)
        self.assertTrue(basic < block < next_panel)
        general = INDEX_HTML.index('id="settings-section-general"')
        models = INDEX_HTML.index('id="settings-section-models"')
        self.assertTrue(general < INDEX_HTML.index('id="settings-avatar"') < models)

    def test_the_agent_form_sends_avatar_style_and_the_banner_uses_the_picture(self) -> None:
        self.assertRegex(APP_JS, r'avatar_style: \$\("agent-avatar-free"\)\.checked \? "free" : "circle"')
        self.assertIn('renderAvatar($("agent-avatar")', APP_JS)
        self.assertIn("renderUserAvatar();", APP_JS)

    def test_styles_cover_circle_free_and_fit(self) -> None:
        self.assertRegex(STYLE_CSS, r"#agent-avatar img[^{]*\{[^}]*object-fit: cover")
        free = re.search(r'#agent-avatar\[data-avatar-style="free"\][^{]*\{([^}]*)\}', STYLE_CSS).group(1)
        for declaration in ("border: 0", "border-radius: 0", "background: none", "overflow: visible"):
            self.assertIn(declaration, free)
        self.assertRegex(STYLE_CSS, r'\[data-avatar-style="free"\] img \{ object-fit: contain')


class AvatarTranslationTests(unittest.TestCase):
    def test_every_new_static_text_has_a_spanish_translation(self) -> None:
        body = APP_JS[APP_JS.index("function translateInterface()"):]
        body = body[:body.index("\n}\n")]
        pairs = re.findall(r'\[\s*"((?:agent|settings)-avatar[a-z-]*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]', body)
        self.assertGreaterEqual(len(pairs), 12)
        for dom_id, text in pairs:
            with self.subTest(id=dom_id):
                self.assertIn(text, messages("es"))
                self.assertIn(f'id="{dom_id}"', INDEX_HTML)

    def test_texts_built_by_the_script_and_the_server_errors_are_translated(self) -> None:
        catalog = messages("es")
        for text in (
            "Use a PNG, JPEG or WEBP image.", "The image is larger than {limit} MiB.",
            "The image is larger than {side} x {side} pixels.", "The file is not a valid PNG, JPEG or WEBP image.",
            "PNG, JPEG or WEBP, up to 2 MiB. Shown at 32 px.", "Save the agent first to add a profile picture.",
            "This agent has no profile picture.", "You have no profile picture.", "This agent id cannot have a profile picture.",
            "Could not save the profile picture; check the data folder permissions.",
            "Could not remove the profile picture; check the data folder permissions.",
            "Could not move the agent's profile picture; close other programs using its files and try again.",
            "Profile picture", "Upload image", "Remove", "No circle or border",
            "For transparent PNGs: the image is shown as it is, without cropping.",
        ):
            with self.subTest(text=text):
                self.assertIn(text, catalog)
                self.assertNotEqual(catalog[text], text)

    def test_the_hint_the_spec_asks_for_is_verbatim(self) -> None:
        self.assertIn("For transparent PNGs: the image is shown as it is, without cropping.", INDEX_HTML)


class AvatarScriptTests(unittest.TestCase):
    def test_saving_an_agent_repaints_the_chat_header_avatar(self) -> None:
        """The header avatar reads the picture style from the agent list: a save that changes it must repaint the header."""
        body = APP_JS[APP_JS.index("async function refreshAgents("):]
        body = body[: body.index("\n}\n")]
        self.assertIn("state.user.agents = me.agents", body)
        self.assertIn("renderAgentBanner()", body)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_app_js_parses(self) -> None:
        subprocess.run(["node", "--check", str(STATIC / "app.js")], check=True)


if __name__ == "__main__":
    unittest.main()
