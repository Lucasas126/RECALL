"""Site icons: the tab icon, iOS/Android home-screen icons, the web manifest and the logo shown next to the name."""

from __future__ import annotations

import json
import re
import struct
import unittest
from pathlib import Path

import test_user_preferences as preferences_tests

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "web" / "static"
ICONS = STATIC / "icons"


def png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    return struct.unpack(">II", data[16:24])


class IconFilesTests(unittest.TestCase):
    def test_png_sizes_match_their_names(self) -> None:
        expected = {
            "favicon-16.png": 16, "favicon-32.png": 32, "apple-touch-icon-180.png": 180,
            "icon-192.png": 192, "icon-512.png": 512, "icon-maskable-192.png": 192, "icon-maskable-512.png": 512,
        }
        for name, side in expected.items():
            self.assertEqual(png_size(ICONS / name), (side, side), name)

    def test_ico_holds_the_three_tab_sizes(self) -> None:
        data = (ICONS / "favicon.ico").read_bytes()
        self.assertEqual(data[:4], b"\x00\x00\x01\x00")
        count = struct.unpack("<H", data[4:6])[0]
        sizes = {(data[6 + 16 * i] or 256) for i in range(count)}
        self.assertEqual(sizes, {16, 32, 48})

    def test_logo_is_an_svg_with_the_pixel_grid_viewbox(self) -> None:
        svg = (ICONS / "recall-logo.svg").read_text(encoding="utf-8")
        self.assertTrue(svg.startswith("<svg "))
        self.assertIn('viewBox="0 0 24 21"', svg)
        self.assertNotIn("<script", svg.lower())

    def test_manifest_icons_exist_and_are_declared_with_their_real_size(self) -> None:
        manifest = json.loads((ICONS / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "RECALL")
        purposes = {icon["purpose"] for icon in manifest["icons"]}
        self.assertEqual(purposes, {"any", "maskable"})
        for icon in manifest["icons"]:
            path = STATIC / icon["src"].removeprefix("/static/")
            self.assertTrue(path.is_file(), icon["src"])
            side = int(icon["sizes"].split("x")[0])
            self.assertEqual(png_size(path), (side, side), icon["src"])


class IconPageTests(unittest.TestCase):
    def test_every_link_in_the_page_head_points_to_an_existing_file(self) -> None:
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        head = page.split("</head>")[0]
        hrefs = re.findall(r'<link rel="(?:icon|apple-touch-icon|manifest)"[^>]*href="([^"]+)"', head)
        self.assertGreaterEqual(len(hrefs), 5)
        for href in hrefs:
            if href == "/favicon.ico":
                continue                    # served by a route, tested below
            self.assertTrue((STATIC / href.removeprefix("/static/")).is_file(), href)

    def test_logo_floats_above_the_login_capital_and_sits_next_to_the_name_in_the_sidebar(self) -> None:
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertEqual(page.count('src="/static/icons/recall-logo.svg"'), 2)
        self.assertRegex(page, r'<div class="art-capital"><img class="brand-logo"[^>]*alt=""[^>]*></div>')
        self.assertRegex(page, r'<span class="brand"><img class="brand-logo"[^>]*alt=""[^>]*><span>RECALL</span></span>')


class IconRoutesTests(unittest.TestCase):
    setUp = preferences_tests.UserPreferencesWebTests.setUp

    def test_root_icons_are_public(self) -> None:
        for path, content_type in (("/favicon.ico", "image/"), ("/apple-touch-icon.png", "image/png")):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertTrue(response.headers["content-type"].startswith(content_type), (path, response.headers["content-type"]))
            self.assertGreater(len(response.content), 100)

    def test_static_icons_and_manifest_are_public(self) -> None:
        for name in ("recall-logo.svg", "favicon-32.png", "manifest.webmanifest"):
            self.assertEqual(self.client.get(f"/static/icons/{name}").status_code, 200, name)
        self.assertEqual(self.client.get("/static/icons/recall-logo.svg").headers["content-type"].split(";")[0], "image/svg+xml")


if __name__ == "__main__":
    unittest.main()
