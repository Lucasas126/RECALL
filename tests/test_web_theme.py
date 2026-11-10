"""Web UI theme: self-hosted fonts, no external requests, no italics, accent contrast and the art slots."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import test_user_preferences as preferences_tests

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "web" / "static"
CSS = (STATIC / "style.css").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "appearance.js").read_text(encoding="utf-8")

ACCENTS = ("pink", "blue", "gold", "green", "violet", "cyan")
FONT_FILES = {"Fraunces": "fraunces.woff2", "Inter": "inter.woff2", "Geist Mono": "geist-mono.woff2"}
ART_FILES = ("arcade.svg", "login-arch.svg", "plinth.svg", "capital.svg")
MIN_CONTRAST = 4.5


def declarations(block: str) -> dict[str, str]:
    return {name: value.strip() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block)}


def luminance(color: str) -> float:
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", color), color
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def block(selector: str, text: str = CSS, indented: bool = False, many: bool = False) -> str:
    """The declarations of the rule(s) whose selector is exactly `selector` (top level unless `indented`)."""
    lead = r"\s*" if indented else ""
    matches = re.findall(rf"(?:^|\n){lead}{re.escape(selector)}\s*\{{([^}}]*)\}}", text)
    assert matches and (many or len(matches) == 1), (selector, len(matches))
    return "\n".join(matches)


def themes() -> dict[str, dict[str, dict[str, str]]]:
    """accent -> theme -> every colour token in effect (neutral tokens + that accent's tokens)."""
    dark = declarations(block(":root", many=True))
    light = declarations(block(':root[data-theme="light"]'))
    result: dict[str, dict[str, dict[str, str]]] = {}
    for accent in ACCENTS:
        dark_tokens, light_tokens = dict(dark), dict(light)
        if accent != "pink":
            dark_tokens.update(declarations(block(f':root[data-accent="{accent}"]')))
            light_tokens.update(declarations(block(f':root[data-accent="{accent}"][data-theme="light"]')))
        result[accent] = {"dark": dark_tokens, "light": light_tokens}
    return result


class FontTests(unittest.TestCase):
    def test_font_files_exist_and_are_licensed(self) -> None:
        for family, name in FONT_FILES.items():
            path = STATIC / "fonts" / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(path.read_bytes()[:4], b"wOF2", name)
        licenses = (STATIC / "fonts" / "LICENSES.md").read_text(encoding="utf-8")
        ofl = (STATIC / "fonts" / "OFL.txt").read_text(encoding="utf-8")
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", ofl)
        for family in FONT_FILES:
            self.assertIn(family, licenses)
            self.assertIn(family.split()[0], ofl)
        self.assertIn("SIL Open Font License 1.1", licenses)
        self.assertIn("https://github.com/", licenses)

    def test_font_faces_use_static_urls_swap_and_upright_style(self) -> None:
        faces = re.findall(r"@font-face\s*\{([^}]*)\}", CSS)
        self.assertEqual(len(faces), len(FONT_FILES))
        for face in faces:
            url = re.search(r'url\("([^"]+)"\)', face).group(1)
            self.assertTrue(url.startswith("/static/fonts/"), url)
            self.assertTrue((STATIC / url.removeprefix("/static/")).is_file(), url)
            self.assertIn("font-display: swap", face)
            self.assertIn("font-style: normal", face)

    def test_font_stacks_have_system_fallbacks(self) -> None:
        tokens = declarations(block(":root", many=True))
        self.assertIn("system-ui", tokens["--sans"])
        self.assertIn("serif", tokens["--serif"])
        self.assertIn("monospace", tokens["--mono"])

    def test_critical_fonts_are_preloaded_with_crossorigin(self) -> None:
        links = re.findall(r'<link rel="preload"[^>]*>', HTML)
        self.assertEqual(len(links), 2)
        for link in links:
            href = re.search(r'href="([^"]+)"', link).group(1)
            self.assertTrue((STATIC / href.removeprefix("/static/")).is_file(), href)
            self.assertIn("crossorigin", link)
            self.assertIn('as="font"', link)


class NoExternalRequestsTests(unittest.TestCase):
    def test_page_loads_nothing_from_other_sites(self) -> None:
        for match in re.finditer(r'<(?:link|script|img|source|iframe)\b[^>]*?\b(?:src|href)="([^"]+)"', HTML):
            self.assertFalse(re.match(r"(?:https?:)?//", match.group(1)), match.group(1))
        self.assertNotIn("@import", CSS)
        without_data_uris = re.sub(r'url\("data:[^"]*"\)', "", CSS)
        self.assertNotRegex(without_data_uris, r"https?://")
        for url in re.findall(r"url\(\s*[\"']?([^\"')]+)", without_data_uris):
            self.assertTrue(url.startswith("/static/"), url)
        for css_in_page in re.findall(r"<style\b[^>]*>(.*?)</style>", HTML, re.S):
            self.assertNotRegex(css_in_page, r"https?://")

    def test_scripts_are_local(self) -> None:
        sources = re.findall(r'<script[^>]*\bsrc="([^"]+)"', HTML)
        self.assertIn("/static/appearance.js", sources)
        for source in sources:
            self.assertTrue(source.startswith("/static/"), source)

    def test_appearance_script_makes_no_requests(self) -> None:
        self.assertNotRegex(JS, r"https?://|fetch\(|XMLHttpRequest")


class NoItalicsTests(unittest.TestCase):
    def test_style_sheet_has_no_italic(self) -> None:
        # No exceptions: agent names, titles, role labels, emphasis, thinking and the empty state are all upright.
        self.assertNotRegex(CSS, r"font-style\s*:\s*(?:italic|oblique)")
        self.assertNotIn("italic", CSS.lower().replace("font-style: normal", ""))


class AccentContrastTests(unittest.TestCase):
    def test_every_accent_defines_both_themes_and_the_system_light_variant(self) -> None:
        light_media = CSS[CSS.index("@media (prefers-color-scheme: light) {\n  :root[data-accent"):]
        for accent in ACCENTS[1:]:
            system = declarations(block(f':root[data-accent="{accent}"]:not([data-theme])', light_media, indented=True))
            explicit = declarations(block(f':root[data-accent="{accent}"][data-theme="light"]'))
            self.assertEqual(system, explicit, accent)

    def test_text_colours_pass_aa_on_every_surface(self) -> None:
        for accent, by_theme in themes().items():
            for theme, t in by_theme.items():
                surfaces = {name: t[name] for name in ("--bg", "--panel", "--raised", "--side")}
                for surface, color in surfaces.items():
                    self.assertGreaterEqual(contrast(t["--text"], color), MIN_CONTRAST, (accent, theme, "text", surface))
                    self.assertGreaterEqual(contrast(t["--muted"], color), MIN_CONTRAST, (accent, theme, "muted", surface))
                for surface in ("--bg", "--panel", "--raised"):
                    self.assertGreaterEqual(contrast(t["--accent"], t[surface]), MIN_CONTRAST, (accent, theme, "accent on", surface))
                    self.assertGreaterEqual(contrast(t["--accent-strong"], t[surface]), MIN_CONTRAST, (accent, theme, "strong on", surface))

    def test_secondary_text_pairs_pass_aa_on_the_real_backgrounds(self) -> None:
        # AGENT label, sidebar sublabels, composer controls, settings hints, login tagline/labels/version: all --muted on these
        for accent, by_theme in themes().items():
            for theme, t in by_theme.items():
                for surface in ("--bg", "--side", "--panel", "--raised", "--code-bg"):
                    self.assertGreaterEqual(contrast(t["--muted"], t[surface]), MIN_CONTRAST, (accent, theme, surface))
        self.assertNotRegex(CSS, r"::placeholder\s*\{[^}]*opacity:\s*0?\.\d")
        self.assertNotRegex(CSS, r"font-size:\s*(?:10|[0-9])px")

    def test_login_inputs_have_accessible_names_and_placeholders(self) -> None:
        login = HTML[HTML.index('<section id="login"'):HTML.index("</section>", HTML.index('<section id="login"'))]
        self.assertIn('id="login-user" placeholder="User" aria-label="User"', login)
        self.assertIn('id="login-password" type="password" placeholder="Password" aria-label="Password"', login)
        self.assertIn('<label for="login-user">User</label>', login)      # the label stays for screen readers
        self.assertIn('<label for="login-password">Password</label>', login)

    def test_assistant_messages_are_an_open_block(self) -> None:
        self.assertRegex(CSS, r"\.msg\.assistant \{[^}]*border: 0;[^}]*background: none;")

    def test_labels_on_accent_buttons_pass_aa_also_when_hovered(self) -> None:
        for accent, by_theme in themes().items():
            for theme, t in by_theme.items():
                self.assertGreaterEqual(contrast(t["--on-accent"], t["--accent"]), MIN_CONTRAST, (accent, theme, "button"))
                self.assertGreaterEqual(contrast(t["--on-accent"], t["--accent-strong"]), MIN_CONTRAST, (accent, theme, "hover"))

    def test_accent_text_on_the_soft_accent_background_passes_aa(self) -> None:
        # memory indicator, task badges, selected tabs: accent text on --accent-soft
        for accent, by_theme in themes().items():
            for theme, t in by_theme.items():
                self.assertGreaterEqual(contrast(t["--accent"], t["--accent-soft"]), MIN_CONTRAST, (accent, theme, "accent on soft"))
                self.assertGreaterEqual(contrast(t["--text"], t["--accent-soft"]), MIN_CONTRAST, (accent, theme, "text on soft"))

    def test_status_colours_pass_aa_on_the_page(self) -> None:
        for accent, by_theme in themes().items():
            for theme, t in by_theme.items():
                for name in ("--danger", "--ok"):
                    self.assertGreaterEqual(contrast(t[name], t["--bg"]), MIN_CONTRAST, (accent, theme, name))
                self.assertGreaterEqual(contrast(t["--warn"], t["--warn-bg"]), MIN_CONTRAST, (accent, theme, "warn"))

    def test_pink_is_the_default_and_the_swatches_match_the_tokens(self) -> None:
        by_accent = themes()
        self.assertEqual(by_accent["pink"]["dark"]["--accent"], "#ff6b9d")
        self.assertEqual(by_accent["pink"]["light"]["--accent"], "#b0205e")
        swatches = re.findall(r'<label class="sw" style="--d:(#\w+);--l:(#\w+)"><input type="radio" name="accent" value="(\w+)"', HTML)
        self.assertEqual([s[2] for s in swatches], list(ACCENTS))
        for dark, light, accent in swatches:
            self.assertEqual(dark, by_accent[accent]["dark"]["--accent"], accent)
            self.assertEqual(light, by_accent[accent]["light"]["--accent"], accent)
        self.assertEqual(re.search(r"const ACCENTS = \[([^\]]+)\]", JS).group(1).replace('"', "").replace(" ", "").split(","), list(ACCENTS))
        self.assertEqual(len(re.findall(r'<input type="radio" name="accent"[^>]*checked', HTML)), 1)


class AppearanceMarkupTests(unittest.TestCase):
    def test_appearance_section_lives_in_the_settings_dialog(self) -> None:
        dialog = HTML[HTML.index('<dialog id="settings-dialog"'):HTML.index("</dialog>", HTML.index('<dialog id="settings-dialog"'))]
        self.assertIn('id="appearance-title">Appearance</h3>', dialog)
        self.assertIn('id="appearance-ornaments"', dialog)
        self.assertEqual(dialog.count('name="accent"'), len(ACCENTS))
        self.assertNotIn('id="appearance"', HTML.replace('id="appearance-', ""))   # no separate popover

    def test_preferences_are_browser_only_and_applied_before_paint(self) -> None:
        self.assertIn('localStorage', JS)
        self.assertIn("dataset.accent", JS)
        self.assertIn("dataset.ornaments", JS)
        head = HTML.split("</head>")[0]
        self.assertIn('<script src="/static/appearance.js"></script>', head)      # blocking, so it runs before first paint
        self.assertLess(head.index("/static/appearance.js"), HTML.index("<body"))


class ArtSlotTests(unittest.TestCase):
    def test_art_files_exist_and_are_referenced_from_the_css_properties(self) -> None:
        for name in ART_FILES:
            path = STATIC / "art" / name
            self.assertTrue(path.is_file(), name)
            svg = path.read_text(encoding="utf-8")
            self.assertTrue(svg.startswith("<svg "), name)
            self.assertNotIn("<script", svg.lower())
            self.assertNotRegex(svg.replace('xmlns="http://www.w3.org/2000/svg"', ""), r"https?://")
        tokens = declarations(block(":root", many=True))
        for slot, extension in (("arcade", "svg"), ("login-arch", "svg"), ("capital", "png"), ("plinth", "svg")):
            self.assertIn(f"/static/art/{slot}.{extension}", tokens[f"--art-{slot}"])
        self.assertTrue((STATIC / "art" / "capital.png").is_file())
        self.assertTrue((STATIC / "art" / "README.md").is_file())

    def test_art_is_only_referenced_through_the_custom_properties(self) -> None:
        outside_root = re.sub(r"(?:^|\n):root \{[^}]*\}", "", CSS)
        self.assertNotIn("/static/art/", outside_root)

    def test_ornaments_are_decorative(self) -> None:
        self.assertIn('<div class="art-arcade" aria-hidden="true"></div>', HTML)
        self.assertIn('<div class="login-art" aria-hidden="true">', HTML)
        self.assertIn('<div class="login-arch" aria-hidden="true"></div>', HTML)
        for selector in (".art-arcade", ".login-arch"):
            rule = next(line for line in CSS.splitlines() if line.startswith(f"{selector} {{"))
            self.assertIn("pointer-events: none", rule, selector)

    def test_login_has_no_cross_or_crest_and_keeps_the_real_ids(self) -> None:
        login = HTML[HTML.index('<section id="login"'):HTML.index("</section>", HTML.index('<section id="login"'))]
        for element_id in ("login-form", "login-user", "login-password", "login-error", "login-version"):
            self.assertIn(f'id="{element_id}"', login)
        self.assertNotIn("crest", login.lower())
        self.assertNotIn("cross", login.lower())
        self.assertLess(login.index("login-arch"), login.index("art-capital"))
        self.assertLess(login.index("art-capital"), login.index("<h1>RECALL</h1>"))
        self.assertNotIn("art-plinth", login)                              # the logo floats above the capital now


class StaticServingTests(unittest.TestCase):
    setUp = preferences_tests.UserPreferencesWebTests.setUp

    def test_fonts_art_and_script_are_public(self) -> None:
        for path, kind in (
            ("/static/fonts/inter.woff2", "font/woff2"), ("/static/fonts/fraunces.woff2", "font/woff2"),
            ("/static/fonts/geist-mono.woff2", "font/woff2"), 
            ("/static/art/capital.svg", "image/svg+xml"), ("/static/appearance.js", "javascript"),
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn(kind, response.headers["content-type"], path)


if __name__ == "__main__":
    unittest.main()
