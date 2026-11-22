from __future__ import annotations

import io
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from src.utils import charts_mermaid
from src.utils.charts_mermaid import (
    INSTALL_HINT, MAX_MERMAID_BYTES, MermaidError, check_mermaid, render_mermaid_png, restore_names, sankey_placeholders,
)

FLOW = "flowchart LR\n A[Start] --> B{Ok?}\n B -->|yes| C[Done]\n B -->|no| A"
SEQUENCE = "sequenceDiagram\n Client->>Server: Request\n Server-->>Client: Response"
GANTT = "gantt\n dateFormat YYYY-MM-DD\n section Plan\n Design :a1, 2026-01-01, 10d\n Build :after a1, 15d"
SANKEY = 'sankey-beta\n\nSalary,Café,300\nSalary,Savings,200\nSalary,"Élan",100\n'
DARK_BG, LIGHT_BG = (0x1B, 0x22, 0x30), (0xFB, 0xF8, 0xF3)


def browser_problem() -> str:
    """Why real renders cannot run here ('' when they can); one trivial render decides."""
    try:
        render_mermaid_png("flowchart LR; A-->B")
    except MermaidError as exc:
        return str(exc)
    return ""


def non_background_pixels(data: bytes, background: tuple[int, int, int]) -> int:
    image = Image.open(io.BytesIO(data)).convert("RGB")
    return sum(count for count, color in image.getcolors(maxcolors=1 << 24) if color != background)


class PureTests(unittest.TestCase):
    def test_sankey_names_become_ascii_ids_and_come_back(self) -> None:
        code, names = sankey_placeholders(SANKEY)
        self.assertTrue(code.isascii())
        self.assertEqual(sorted(names.values()), ["Café", "Salary", "Savings", "Élan"])
        self.assertEqual(code.splitlines()[0], "sankey-beta")
        self.assertEqual(len([line for line in code.splitlines() if line.strip()]), 4)
        ids = {key: value for key, value in names.items()}
        svg = '<g><text>' + next(k for k, v in ids.items() if v == "Café") + ' 300</text><text>' + next(k for k, v in ids.items() if v == "Élan") + '</text></g>'
        restored = restore_names(svg, names)
        self.assertIn("Café 300", restored)
        self.assertIn(">Élan<", restored)
        self.assertNotIn("Nzq", restored)

    def test_restore_escapes_markup_and_keeps_unknown_text(self) -> None:
        _, names = sankey_placeholders("sankey-beta\n\n\"A & B\",C,1\n")
        key = next(k for k, v in names.items() if v == "A & B")
        self.assertEqual(restore_names(f"<text>{key}</text><text>other</text>", names), "<text>A &amp; B</text><text>other</text>")

    def test_bad_sankey_row(self) -> None:
        with self.assertRaises(MermaidError):
            sankey_placeholders("sankey-beta\n\nA,B\n")

    def test_source_checks(self) -> None:
        for bad in ("", "  ", None, 5):
            with self.assertRaises(MermaidError):
                check_mermaid(bad)
        with self.assertRaises(MermaidError) as caught:
            check_mermaid("a" * (MAX_MERMAID_BYTES + 1))
        self.assertIn("larger than", str(caught.exception))

    def test_unknown_theme(self) -> None:
        with self.assertRaises(ValueError):
            render_mermaid_png(FLOW, "neon")

    def test_missing_browser_is_an_actionable_error(self) -> None:
        done = subprocess.CompletedProcess([], charts_mermaid.EXIT_BROWSER_MISSING)
        proc = mock.Mock(returncode=done.returncode)
        proc.communicate.return_value = (b"", b"Executable doesn't exist")
        with mock.patch.object(charts_mermaid.subprocess, "Popen", return_value=proc), \
                mock.patch.object(charts_mermaid.os, "killpg"):
            with self.assertRaises(MermaidError) as caught:
                render_mermaid_png(FLOW)
        self.assertEqual(str(caught.exception), INSTALL_HINT)
        self.assertIn("apt install", INSTALL_HINT)

    def test_timeout_kills_the_browser_group(self) -> None:
        proc = mock.Mock(pid=4242, returncode=-9)
        proc.communicate.side_effect = [subprocess.TimeoutExpired("x", 1), (b"", b"")]
        with mock.patch.object(charts_mermaid.subprocess, "Popen", return_value=proc), \
                mock.patch.object(charts_mermaid.os, "killpg") as killpg:
            with self.assertRaises(MermaidError) as caught:
                render_mermaid_png(FLOW)
        self.assertIn("longer than", str(caught.exception))
        killpg.assert_called()

    def test_worker_failure_has_one_line_reason(self) -> None:
        proc = mock.Mock(returncode=charts_mermaid.EXIT_BAD_SOURCE)
        proc.communicate.return_value = (b"", b"Traceback ignored\nParse error on line 2")
        with mock.patch.object(charts_mermaid.subprocess, "Popen", return_value=proc), \
                mock.patch.object(charts_mermaid.os, "killpg"):
            with self.assertRaises(MermaidError) as caught:
                render_mermaid_png(FLOW)
        self.assertEqual(str(caught.exception), "invalid Mermaid: Parse error on line 2")

    def test_sandbox_is_only_disabled_when_needed(self) -> None:
        with mock.patch.object(charts_mermaid.os, "geteuid", return_value=1000), \
                mock.patch.object(charts_mermaid.Path, "exists", return_value=False), \
                mock.patch.dict(charts_mermaid.os.environ, {}, clear=True):
            self.assertFalse(charts_mermaid.needs_no_sandbox())
        with mock.patch.object(charts_mermaid.os, "geteuid", return_value=0):
            self.assertTrue(charts_mermaid.needs_no_sandbox())

    def test_vendored_files_exist(self) -> None:
        folder = Path(charts_mermaid.MERMAID_JS).parent
        self.assertTrue(charts_mermaid.MERMAID_JS.is_file())
        self.assertIn("MIT License", (folder / "mermaid.LICENSE").read_text(encoding="utf-8"))


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.problem = browser_problem()

    def setUp(self) -> None:
        if self.problem:
            self.skipTest(f"Chromium is not usable here ({self.problem[:150]}); install chromium-headless-shell to run these tests")

    def test_flowchart_dark(self) -> None:
        data = render_mermaid_png(FLOW)
        self.assertEqual(Image.open(io.BytesIO(data)).getpixel((2, 2))[:3], DARK_BG)
        self.assertGreater(non_background_pixels(data, DARK_BG), 3000)

    def test_sequence_has_text(self) -> None:
        data = render_mermaid_png(SEQUENCE)
        self.assertGreater(non_background_pixels(data, DARK_BG), 3000)

    def test_gantt_light(self) -> None:
        data = render_mermaid_png(GANTT, "light")
        self.assertEqual(Image.open(io.BytesIO(data)).getpixel((2, 2))[:3], LIGHT_BG)
        self.assertGreater(non_background_pixels(data, LIGHT_BG), 3000)

    def test_sankey_with_accents(self) -> None:
        data = render_mermaid_png(SANKEY)
        self.assertGreater(non_background_pixels(data, DARK_BG), 3000)

    def test_text_is_not_blank(self) -> None:
        """Without the embedded fonts the labels vanish: a node's text colour must be present."""
        data = render_mermaid_png("flowchart LR\n A[Some long label here]")
        image = Image.open(io.BytesIO(data)).convert("RGB")
        light_text = sum(1 for r, g, b in zip(*[iter(image.tobytes())] * 3) if r > 200 and g > 200 and b > 190)
        self.assertGreater(light_text, 150)

    def test_invalid_source_is_a_clean_error(self) -> None:
        with self.assertRaises(MermaidError) as caught:
            render_mermaid_png("flowchart LR\n A --> --> B ((")
        message = str(caught.exception)
        self.assertTrue(message.startswith("invalid Mermaid:"))
        self.assertNotIn("Traceback", message)
        self.assertLess(len(message), 500)

    def test_every_diagram_in_the_manual_renders(self) -> None:
        page = Path(__file__).resolve().parents[2] / "src" / "knowledge" / "recall" / "references" / "charts.md"
        blocks = re.findall(r"```mermaid\n(.*?)\n```", page.read_text(encoding="utf-8"), re.S)
        self.assertGreaterEqual(len(blocks), 4)
        for block in blocks:
            with self.subTest(block=block[:20]):
                self.assertTrue(render_mermaid_png(block).startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
