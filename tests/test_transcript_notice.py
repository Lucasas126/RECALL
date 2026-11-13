import unittest
from pathlib import Path


class TranscriptNoticeTests(unittest.TestCase):
    def test_web_shows_transcript_and_mic_does_not_overlap(self):
        chat = Path("src/service/chat.py").read_text()
        self.assertIn('if transcripts and channel == "web"', chat)
        css = Path("src/web/static/style.css").read_text()
        self.assertIn("#mic.recording { width: auto", css)


if __name__ == "__main__":
    unittest.main()
