from __future__ import annotations

import unittest

from src.agent.toolsets.common import READ_LINE_CHARS, READ_MAX_CHARS, cap_text


class CapTextTests(unittest.TestCase):
    def test_short_text_is_returned_as_is(self) -> None:
        self.assertEqual(cap_text("a\nb\nc"), "a\nb\nc")
        self.assertEqual(cap_text("a\nb\nc", [2, 3]), "b\nc")
        self.assertEqual(cap_text("a\nb\nc", [2]), "b")

    def test_long_text_stops_at_a_whole_line_and_says_how_to_continue(self) -> None:
        rows = [f"{n:05d}" + "x" * 94 for n in range(1, 1001)]  # 100 chars + newline each
        out = cap_text("\n".join(rows))
        self.assertLessEqual(len(out), READ_MAX_CHARS + 300)
        shown = len(out.split("\n…[")[0].split("\n"))
        self.assertGreater(shown, 300)
        self.assertTrue(f"showed lines 1-{shown} of 1000" in out)
        self.assertTrue(f"continue with lines=[{shown + 1}, {shown + 200}]" in out)
        self.assertTrue(out.split("\n")[shown - 1].startswith(f"{shown:05d}"))

    def test_a_range_is_capped_too_and_continues_inside_it(self) -> None:
        rows = ["y" * 1000 for _ in range(500)]
        out = cap_text("\n".join(rows), [101, 450])
        self.assertTrue("of 500" in out)
        self.assertTrue("continue with lines=[" in out)
        self.assertFalse(", 500]" in out)

    def test_huge_lines_are_cut_with_a_marker(self) -> None:
        out = cap_text("short\n" + "z" * 900_000 + "\nend")
        self.assertTrue("…[line 2 cut: 900000 chars]" in out)
        self.assertTrue("1 long line(s) cut" in out)
        self.assertTrue(out.splitlines()[2] == "end")
        self.assertLess(len(out), READ_LINE_CHARS + 500)

    def test_one_huge_line_alone_is_still_shown_cut(self) -> None:
        out = cap_text("q" * 100_000)
        self.assertTrue(out.startswith("q" * READ_LINE_CHARS))


if __name__ == "__main__":
    unittest.main()
