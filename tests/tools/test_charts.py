from __future__ import annotations

import io
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from PIL import Image

from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_CATALOG, TOOL_GROUP_TOOLS, validate_tool_groups
from src.agent.tools import build_tools, chat_folder_options
from src.config.levels import Level
from src.config.models import FolderConfig
from src.core.sources.registry import SourceRegistry
from src.utils import charts_render
from src.utils.charts_render import ChartError, check_chart_spec, render_chart_png, render_dot_png, themed_dot
from src.utils.charts_theme import THEMES, get_theme, themed_spec

HAS_DOT = shutil.which("dot") is not None
BAR = {
    "title": "Spending by category",
    "data": {"values": [{"c": "Housing", "v": 900}, {"c": "Food", "v": 450}, {"c": "Travel", "v": 200}]},
    "mark": "bar",
    "encoding": {"x": {"field": "c", "type": "nominal", "sort": "-y"}, "y": {"field": "v", "type": "quantitative"}},
}
CANDLES = {
    "title": "Price",
    "data": {"values": [
        {"d": "Mon", "lo": 8, "hi": 14, "o": 10, "c": 12, "dir": "Up"},
        {"d": "Tue", "lo": 7, "hi": 13, "o": 12, "c": 9, "dir": "Down"},
    ]},
    "encoding": {"x": {"field": "d", "type": "nominal"},
                 "color": {"field": "dir", "type": "nominal", "scale": {"domain": ["Up", "Down"], "range": ["#8AB17D", "#E76F51"]}}},
    "layer": [
        {"mark": "rule", "encoding": {"y": {"field": "lo", "type": "quantitative", "scale": {"zero": False}}, "y2": {"field": "hi"}}},
        {"mark": {"type": "bar", "size": 6}, "encoding": {"y": {"field": "o", "type": "quantitative"}, "y2": {"field": "c"}}},
    ],
}
WATERFALL = {
    "title": "Budget",
    "data": {"values": [
        {"n": "Salary", "a": 0, "b": 3000, "t": "Income", "o": 1},
        {"n": "Rent", "a": 3000, "b": 2000, "t": "Expense", "o": 2},
        {"n": "Left", "a": 0, "b": 2000, "t": "Total", "o": 3},
    ]},
    "mark": "bar",
    "encoding": {
        "x": {"field": "n", "type": "nominal", "sort": {"field": "o"}},
        "y": {"field": "a", "type": "quantitative"}, "y2": {"field": "b"},
        "color": {"field": "t", "type": "nominal", "scale": {"domain": ["Income", "Expense", "Total"], "range": ["#8AB17D", "#E76F51", "#E9C46A"]}},
    },
}
DOT = 'digraph G { rankdir=LR; node [shape=box, style="rounded,filled"]; Input -> Process [label="data"]; Process -> Output }'


def text_pixels(data: bytes, top: int = 70) -> int:
    """Pixels in the title band that differ from the background: text exists only if fonts were registered."""
    image = Image.open(io.BytesIO(data)).convert("RGB")
    background = image.getpixel((2, 2))
    band = image.crop((0, 0, image.width, top))
    return sum(count for count, color in band.getcolors(maxcolors=1 << 24) if color != background)


class ThemeTests(unittest.TestCase):
    def test_default_is_dark_and_themes_are_known(self) -> None:
        self.assertEqual(get_theme("")["background"], "#1B2230")
        self.assertEqual(get_theme("LIGHT")["background"], "#FBF8F3")
        self.assertEqual(set(THEMES), {"dark", "light"})
        with self.assertRaises(ValueError):
            get_theme("neon")

    def test_agent_config_overrides_the_theme_and_the_rest_is_kept(self) -> None:
        spec = {"mark": "bar", "config": {"background": "#FFFFFF", "axis": {"labelFontSize": 20}}}
        result = themed_spec(spec, get_theme("dark"))
        self.assertEqual(result["config"]["background"], "#FFFFFF")
        self.assertEqual(result["config"]["axis"]["labelFontSize"], 20)
        self.assertEqual(result["config"]["axis"]["labelColor"], "#ECE7DD")
        self.assertEqual(result["config"]["font"], "Open Sans")
        self.assertEqual(result["padding"], 16)
        self.assertEqual(spec["config"]["background"], "#FFFFFF")  # the input is not mutated
        self.assertNotIn("padding", spec)
        self.assertEqual(themed_spec({"padding": 4}, get_theme("light"))["padding"], 4)

    def test_dot_defaults_come_first_so_explicit_attributes_win(self) -> None:
        out = themed_dot('digraph "a{b" { // {\n node [color=red]; a -> b }')
        self.assertLess(out.index("bgcolor"), out.index("color=red"))
        self.assertIn("#2A3550", out)
        self.assertTrue(out.startswith('digraph "a{b" {\n graph ['))
        self.assertTrue(out.endswith(' // {\n node [color=red]; a -> b }'))


class ValidationTests(unittest.TestCase):
    def test_bad_specs(self) -> None:
        for spec, expected in [
            ("not a dict", "must be a Vega-Lite JSON object"),
            ([BAR], "must be a Vega-Lite JSON object"),
            ({}, "empty"),
            ({"data": {"url": "https://example.com/a.csv"}, "mark": "bar"}, "data.url"),
            ({"layer": [{"data": {"url": "file:///etc/hostname"}}]}, "layer[0].data.url"),
            ({"transform": [{"lookup": "a", "from": {"data": {"url": "x.json"}, "key": "k"}}]}, "url"),
            ({"mark": {"type": "image", "url": "x.png"}}, "mark.url"),
            ({"mark": "bar", "width": 5000}, "'width'"),
            ({"data": {"values": ["x" * 1_100_000]}}, "limit"),
        ]:
            with self.subTest(expected=expected), self.assertRaises(ChartError) as caught:
                check_chart_spec(spec)
            self.assertIn(expected, str(caught.exception))

    def test_a_url_field_name_inside_inline_rows_is_fine(self) -> None:
        check_chart_spec({"data": {"values": [{"url": "a", "n": 1}]}, "mark": "bar"})

    def test_bad_sizes(self) -> None:
        with self.assertRaises(ChartError):
            render_chart_png(BAR, width=9000)

    def test_dot_file_attributes_are_refused(self) -> None:
        for source in ('digraph { a [image="x.png"] }', 'digraph { imagepath="/"; a }', 'digraph { a [shapefile = "x"] }',
                       'digraph { graph [fontpath="/x"]; a }', 'digraph { a [label=<<img src="x.png"/>>] }',
                       'digraph { a ["Image"="x"] }'):
            with self.subTest(source=source), self.assertRaises(ChartError) as caught:
                render_dot_png(source)
            self.assertIn("files", str(caught.exception))
        with self.assertRaises(ChartError):
            render_dot_png("x" * 300_000)
        with self.assertRaises(ChartError):
            render_dot_png("   ")

    def test_missing_graphviz_is_an_actionable_error(self) -> None:
        with mock.patch.object(charts_render.shutil, "which", return_value=None), self.assertRaises(ChartError) as caught:
            render_dot_png(DOT)
        self.assertIn("Graphviz is not installed", str(caught.exception))
        self.assertIn("apt install graphviz", str(caught.exception))

    def test_not_a_graph_is_reported(self) -> None:
        with self.assertRaises(ChartError):
            render_dot_png("a -> b")


class RenderTests(unittest.TestCase):
    def assert_png_with_text(self, data: bytes) -> None:
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertGreater(text_pixels(data), 200)

    def test_bar_chart(self) -> None:
        data = render_chart_png(BAR)
        self.assert_png_with_text(data)
        self.assertEqual(Image.open(io.BytesIO(data)).getpixel((2, 2))[:3], (0x1B, 0x22, 0x30))

    def test_light_theme_background(self) -> None:
        data = render_chart_png(BAR, theme="light")
        self.assertEqual(Image.open(io.BytesIO(data)).getpixel((2, 2))[:3], (0xFB, 0xF8, 0xF3))

    def test_candlestick_and_waterfall(self) -> None:
        self.assert_png_with_text(render_chart_png(CANDLES))
        self.assert_png_with_text(render_chart_png(WATERFALL))

    def test_width_and_height_set_the_size(self) -> None:
        small = Image.open(io.BytesIO(render_chart_png({**BAR, "width": 100}, width=300, height=200)))
        self.assertGreater(small.width, 600)
        self.assertLess(small.width, 1000)

    @unittest.skipUnless(HAS_DOT, "Graphviz (`dot`) is not installed; install it with `apt install graphviz` to run this test")
    def test_graphviz_diagram(self) -> None:
        data = render_dot_png(DOT)
        image = Image.open(io.BytesIO(data)).convert("RGB")
        self.assertEqual(image.getpixel((2, 2)), (0x1B, 0x22, 0x30))
        self.assertGreater(sum(count for count, color in image.getcolors(maxcolors=1 << 24) if color != (0x1B, 0x22, 0x30)), 2000)
        self.assertGreater(text_pixels(data, top=image.height), 500)

    @unittest.skipUnless(HAS_DOT, "Graphviz (`dot`) is not installed")
    def test_explicit_dot_attributes_win(self) -> None:
        data = render_dot_png('digraph { bgcolor="#FF0000"; a -> b }')
        self.assertEqual(Image.open(io.BytesIO(data)).convert("RGB").getpixel((2, 2)), (255, 0, 0))

    @unittest.skipUnless(HAS_DOT, "Graphviz (`dot`) is not installed")
    def test_invalid_dot_is_an_error(self) -> None:
        with self.assertRaises(ChartError) as caught:
            render_dot_png("digraph { a -> }")
        self.assertIn("invalid DOT", str(caught.exception))

    def test_timeout_is_reported(self) -> None:
        import subprocess
        with mock.patch.object(charts_render.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            with self.assertRaises(ChartError) as caught:
                render_chart_png(BAR)
        self.assertIn("longer than", str(caught.exception))


class ManualTests(unittest.TestCase):
    def test_every_recipe_in_the_manual_renders(self) -> None:
        import json
        import re
        page = Path(__file__).resolve().parents[2] / "src" / "knowledge" / "recall" / "references" / "charts.md"
        recipes = re.findall(r"```json\n(.*?)\n```", page.read_text(encoding="utf-8"), re.S)
        self.assertGreaterEqual(len(recipes), 9)
        for recipe in recipes:
            with self.subTest(recipe=recipe[:40]):
                self.assertTrue(render_chart_png(json.loads(recipe)).startswith(b"\x89PNG"))


class ChartToolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        folders = [FolderConfig(name="notes", path=str(self.notes)), FolderConfig(name="work", path=str(self.work), read_only=True)]
        groups = ["memory_read", "memory_edit", "folders_edit", "charts"]
        options = chat_folder_options(SimpleNamespace(folders=folders, tool_groups=groups, tools=True, recall_knowledge=False))
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=self.root / "data", **options)
        self.written: list[str] = []
        self.host = SimpleNamespace(sources=self.sources, config=SimpleNamespace(level=Level.ADMIN), index=None,
                                    note_written=self.written.append)
        self.tools = build_tools(self.host, None, groups)[1]
        self.palace = self.root / "data" / "u" / "a" / "mind_palace"

    def test_default_location_and_free_address(self) -> None:
        first = self.tools["render_chart"](spec=BAR)
        second = self.tools["render_chart"](spec=BAR)
        self.assertIn("saved mind_palace:charts/spending-by-category.png (", first)
        self.assertIn("px", first)
        self.assertIn("charts/spending-by-category-2.png", second)
        self.assertTrue((self.palace / "charts" / "spending-by-category.png").read_bytes().startswith(b"\x89PNG"))
        self.assertIn("mind_palace:charts/spending-by-category.png", self.written)
        again = self.tools["render_chart"](spec=BAR, overwrite=True)
        self.assertIn("charts/spending-by-category.png", again)
        self.assertFalse((self.palace / "charts" / "spending-by-category-3.png").exists())

    def test_path_and_source_errors_write_nothing(self) -> None:
        run = self.tools["render_chart"]
        self.assertIn("saved notes:Reports/a.png", run(spec=BAR, path="notes:Reports/a.png"))
        self.assertTrue((self.notes / "Reports" / "a.png").exists())
        self.assertIn("read-only", run(spec=BAR, path="work:a.png"))
        self.assertFalse((self.work / "a.png").exists())
        self.assertIn(".png", run(spec=BAR, path="notes:a.txt"))
        self.assertIn("escapes", run(spec=BAR, path="notes:../a.png"))
        self.assertTrue(run(spec=BAR, path="nowhere:a.png").startswith("error:"))

    def test_validation_errors_are_returned(self) -> None:
        run = self.tools["render_chart"]
        self.assertTrue(run(spec="text").startswith("error: 'spec' must be"))
        self.assertIn("data.url", run(spec={"data": {"url": "http://example.com/x.json"}, "mark": "bar"}))
        self.assertIn("unknown theme", run(spec=BAR, theme="neon"))
        self.assertFalse((self.palace / "charts").exists())

    def test_mermaid_is_planned_and_unknown_kinds_are_refused(self) -> None:
        run = self.tools["render_diagram"]
        self.assertIn("not available in this build yet", run(kind="mermaid", source="graph TD; A-->B"))
        self.assertIn("unknown kind", run(kind="plantuml", source="x"))
        self.assertFalse((self.palace / "charts").exists())

    @unittest.skipUnless(HAS_DOT, "Graphviz (`dot`) is not installed")
    def test_diagram_is_saved_and_dot_missing_is_reported(self) -> None:
        result = self.tools["render_diagram"](kind="graphviz", source=DOT)
        self.assertIn("saved mind_palace:charts/diagram.png", result)
        self.assertIn("mind_palace:charts/diagram.png", self.written)
        with mock.patch.object(charts_render.shutil, "which", return_value=None):
            self.assertIn("Graphviz is not installed", self.tools["render_diagram"](kind="graphviz", source=DOT))

    def test_the_group_exists_and_gates_the_tools(self) -> None:
        self.assertIn("charts", {item["id"] for item in TOOL_GROUP_CATALOG})
        self.assertEqual(validate_tool_groups(["charts"]), ["charts"])
        self.assertEqual(TOOL_GROUP_TOOLS["charts"], ("render_chart", "render_diagram"))
        self.assertTrue({"render_chart", "render_diagram"} <= DISPOSABLE_BLOCKED_TOOLS)
        for groups in (["memory_read", "memory_edit"], []):
            handlers = build_tools(self.host, None, groups)[1]
            self.assertNotIn("render_chart", handlers)
            self.assertNotIn("render_diagram", handlers)
        specs = build_tools(self.host, None, ["charts"])[0]
        self.assertEqual([s["function"]["name"] for s in specs], ["render_chart", "render_diagram"])

    def test_not_in_a_disposable_chat_or_below_assistant(self) -> None:
        self.assertNotIn("render_chart", build_tools(self.host, None, ["charts"], disposable=True)[1])
        low = SimpleNamespace(sources=self.sources, config=SimpleNamespace(level=Level.READER), index=None)
        self.assertNotIn("render_chart", build_tools(low, None, ["charts"])[1])


if __name__ == "__main__":
    unittest.main()
