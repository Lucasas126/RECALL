"""`api()` in app.js passes `body` to fetch unchanged, so a JSON body must already be a string."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[2] / "src" / "web" / "static" / "app.js"


class ApiJsonBodyTests(unittest.TestCase):
    def test_no_call_sends_a_raw_object_as_body(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        offenders = [
            f"line {source.count(chr(10), 0, match.start()) + 1}: {match.group(0)}"
            for match in re.finditer(r"\bbody:\s*\{", source)
        ]
        self.assertEqual(offenders, [], "wrap JSON bodies in JSON.stringify(...)")


if __name__ == "__main__":
    unittest.main()
