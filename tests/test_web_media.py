from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_chat_models as models_tests
from src.utils.i18n import InputError, error_message

_STATIC = Path(__file__).parent.parent / "src" / "web" / "static"
_AUDIO = {
    "modality": "audio", "name": "voice.webm", "mime_type": "audio/webm;codecs=opus", "format": "webm",
    "data_url": "data:audio/webm;base64," + "QUJD" * 12,
}


class VoiceMessageServiceTests(models_tests.ModelFixture):
    def _send_audio(self, transcript: str):
        resolved = Mock(provider_name="openrouter", api_key="test")
        transcriber = Mock()
        transcriber.transcribe.return_value = transcript
        with patch("src.service.chat.load_agent", return_value=resolved), \
                patch("src.service.chat.OpenRouterProvider", return_value=transcriber):
            return list(self.service.send("user-one", "agent-one", self.chat, "", attachments=[dict(_AUDIO)]))

    def test_audio_stays_on_the_turn_so_it_can_be_played_back(self) -> None:
        self._send_audio("Hello there")
        turn = self.service.get_chat("user-one", "agent-one", self.chat)["messages"][0]
        self.assertIn("Hello there", turn["content"])
        self.assertEqual(turn["media"], [{
            "name": "voice.webm", "modality": "audio", "mime_type": "audio/webm;codecs=opus",
            "data_url": _AUDIO["data_url"],
        }])

    def test_empty_transcription_gives_a_clear_error_and_logs_it(self) -> None:
        with self.assertLogs("recall.service", level="WARNING") as logs:
            with self.assertRaises(InputError) as caught:
                self._send_audio("")
        self.assertTrue(error_message("es", caught.exception).startswith("No se ha detectado voz en «voice.webm»."))
        self.assertIn("returned no text", logs.output[0])
        self.assertIn("webm", logs.output[0])
        self.assertEqual(self.service.get_chat("user-one", "agent-one", self.chat)["messages"], [])

    def test_successful_transcription_is_logged_with_format_and_size(self) -> None:
        with self.assertLogs("recall.service", level="INFO") as logs:
            self._send_audio("Hello")
        line = next(l for l in logs.output if "transcribed audio" in l)
        self.assertIn("format=webm", line)
        self.assertIn("36 bytes", line)
        self.assertIn("5 characters", line)


class WebMediaStaticTests(unittest.TestCase):
    def test_attachments_play_in_the_message_and_recording_shows_the_time(self) -> None:
        script = (_STATIC / "app.js").read_text(encoding="utf-8")
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        style = (_STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn("function attachmentView", script)
        self.assertIn("media.controls = true", script)
        self.assertIn("m.media", script)
        self.assertIn('id="mic-time"', html)
        self.assertIn('$("mic-time").textContent = time', script)
        self.assertIn(".mic-time", style)
        self.assertIn('$("input").addEventListener("paste"', script)
        self.assertIn("async function addFiles", script)


if __name__ == "__main__":
    unittest.main()
