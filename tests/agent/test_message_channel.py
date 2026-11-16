from __future__ import annotations

import unittest
from pathlib import Path

from src.agent.agent import Agent

_STATIC = Path(__file__).parent.parent.parent / "src" / "web" / "static"


class MessageChannelTests(unittest.TestCase):
    TS = "2026-11-13T10:07:12+01:00"

    def test_text_turn_names_the_channel(self) -> None:
        self.assertEqual(Agent._stamped("hi", self.TS, "telegram"), "[2026-11-13 10:07+01:00 via telegram] hi")
        self.assertEqual(Agent._stamped("hi", self.TS, "web"), "[2026-11-13 10:07+01:00 via web UI] hi")

    def test_turn_without_channel_keeps_the_plain_stamp(self) -> None:
        self.assertEqual(Agent._stamped("hi", self.TS), "[2026-11-13 10:07+01:00] hi")
        self.assertEqual(Agent._stamped("", self.TS, None), "[2026-11-13 10:07+01:00]")

    def test_multipart_turn_prefixes_the_first_text_part(self) -> None:
        parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        stamped = Agent._stamped(parts, self.TS, "telegram")
        self.assertEqual(stamped[0], {"type": "text", "text": "[2026-11-13 10:07+01:00 via telegram]"})
        self.assertEqual(stamped[1:], parts)


class VoiceButtonStaticTests(unittest.TestCase):
    def test_composer_has_a_mic_button_wired_to_the_recorder(self) -> None:
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        script = (_STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="mic"', html)
        for needle in ("getUserMedia", "new MediaRecorder", '$("mic").addEventListener("click"'):
            self.assertIn(needle, script)


if __name__ == "__main__":
    unittest.main()
