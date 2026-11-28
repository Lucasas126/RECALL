import unittest
from pathlib import Path


class PersistentTranscriptTests(unittest.TestCase):
    def test_the_transcript_is_stored_on_the_audio_media_and_shown_by_the_page(self):
        chat = Path("src/service/chat.py").read_text()
        self.assertIn('item["transcript"] = heard', chat)
        self.assertNotIn("🎤", chat)
        js = Path("src/web/static/app.js").read_text()
        self.assertIn("item.transcript", js)
        self.assertNotIn("attachment-transcript", js)  # the heard text is message text, not a caption under the player
        css = Path("src/web/static/style.css").read_text()
        self.assertIn("#mic.recording { width: auto", css)


if __name__ == "__main__":
    unittest.main()
