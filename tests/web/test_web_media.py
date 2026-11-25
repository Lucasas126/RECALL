from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.chat import test_chat_models as models_tests
from src.utils.i18n import InputError, error_message

_STATIC = Path(__file__).parent.parent.parent / "src" / "web" / "static"
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
            "data_url": _AUDIO["data_url"], "transcript": "Hello there",
        }])

    def test_transcript_survives_reloading_the_chat(self) -> None:
        self._send_audio("Hola qué tal")
        reloaded = self.service.get_chat("user-one", "agent-one", self.chat)["messages"][0]
        self.assertEqual(reloaded["media"][0]["transcript"], "Hola qué tal")

    def test_transcript_event_comes_before_the_reply(self) -> None:
        events = self._send_audio("Hello there")
        heard = [e for e in events if e.kind == "transcript"]
        self.assertEqual([e.data for e in heard], [{"transcripts": ["Hello there"]}])
        self.assertEqual(events[0].kind, "transcript")

    def test_editing_a_voice_message_keeps_recording_and_transcript(self) -> None:
        self._send_audio("Hello there")
        resolved = Mock(provider_name="openrouter", api_key="test")
        with patch("src.service.chat.load_agent", return_value=resolved):
            events = list(self.service.send("user-one", "agent-one", self.chat, "and more", rewind_to=0))
        self.assertEqual([e for e in events if e.kind == "transcript"], [])  # nothing is transcribed again
        turn = self.service.get_chat("user-one", "agent-one", self.chat)["messages"][0]
        self.assertEqual(turn["content"], "and more\n\n[AUDIO] Audio transcription:\nHello there")
        self.assertEqual([m["transcript"] for m in turn["media"]], ["Hello there"])

    def _edit(self, text: str, recordings):
        resolved = Mock(provider_name="openrouter", api_key="test")
        with patch("src.service.chat.load_agent", return_value=resolved):
            list(self.service.send("user-one", "agent-one", self.chat, text, rewind_to=0, recordings=recordings))
        return self.service.get_chat("user-one", "agent-one", self.chat)["messages"][0]

    def test_editing_the_heard_text_changes_what_the_model_and_the_page_get(self) -> None:
        self._send_audio("Hello there")
        turn = self._edit("and more", ["Hello, Alex"])
        self.assertEqual(turn["content"], "and more\n\n[AUDIO] Audio transcription:\nHello, Alex")
        self.assertEqual([m["transcript"] for m in turn["media"]], ["Hello, Alex"])
        self.assertEqual(turn["media"][0]["data_url"], _AUDIO["data_url"])

    def test_heard_text_edited_inside_the_message_leaves_a_plain_player(self) -> None:
        self._send_audio("Hello there")
        resolved = Mock(provider_name="openrouter", api_key="test")
        with patch("src.service.chat.load_agent", return_value=resolved):
            list(self.service.send("user-one", "agent-one", self.chat, "mine\n\nHello, Alex", rewind_to=0,
                                   recordings=["Hello there"], transcripts_in_text=True))
        turn = self.service.get_chat("user-one", "agent-one", self.chat)["messages"][0]
        self.assertEqual(turn["content"], "mine\n\nHello, Alex")  # nothing appended
        self.assertEqual(len(turn["media"]), 1)
        self.assertNotIn("transcript", turn["media"][0])

    def test_removing_a_recording_drops_it_and_its_text(self) -> None:
        self._send_audio("Hello there")
        turn = self._edit("just text", [None])
        self.assertEqual(turn["content"], "just text")
        self.assertEqual(turn.get("media") or [], [])

    def test_a_blank_heard_text_keeps_a_plain_player(self) -> None:
        self._send_audio("Hello there")
        turn = self._edit("just text", [""])
        self.assertEqual(turn["content"], "just text")
        self.assertEqual(len(turn["media"]), 1)
        self.assertNotIn("transcript", turn["media"][0])

    def test_removing_the_only_recording_of_an_empty_message_is_refused(self) -> None:
        self._send_audio("Hello there")
        before = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        resolved = Mock(provider_name="openrouter", api_key="test")
        with patch("src.service.chat.load_agent", return_value=resolved), self.assertRaises(InputError):
            self.service.send("user-one", "agent-one", self.chat, "", rewind_to=0, recordings=[None])
        self.assertEqual(self.service.get_chat("user-one", "agent-one", self.chat)["messages"], before)

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


class SentPdfServiceTests(models_tests.ModelFixture):
    _PDF = {
        "modality": "document", "name": "informe.pdf", "mime_type": "application/pdf", "format": "pdf",
        "data_url": "data:application/pdf;base64," + "QUJD" * 12,
    }

    def _send_pdf(self, **extra):
        with patch("src.service.chat.extract_pdf_text") as extract:
            extract.return_value.render.return_value = "page text"
            return list(self.service.send("user-one", "agent-one", self.chat, "mira", attachments=[dict(self._PDF)], **extra))

    def test_sent_pdf_stays_on_the_turn_so_it_can_be_opened_again(self) -> None:
        self._send_pdf()
        turn = self.service.get_chat("user-one", "agent-one", self.chat)["messages"][0]
        self.assertIn("page text", turn["content"])  # the model still gets the text
        self.assertEqual(turn["media"], [{
            "name": "informe.pdf", "modality": "document", "mime_type": "application/pdf",
            "data_url": self._PDF["data_url"],
        }])

    def test_editing_the_message_keeps_the_pdf(self) -> None:
        self._send_pdf()
        with patch("src.service.chat.extract_pdf_text") as extract:
            extract.return_value.render.return_value = "page text"
            list(self.service.send("user-one", "agent-one", self.chat, "mira otra vez", rewind_to=0))
        turn = self.service.get_chat("user-one", "agent-one", self.chat)["messages"][0]
        self.assertEqual([m["name"] for m in turn["media"]], ["informe.pdf"])


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


class AgentFilesPlacementTests(unittest.TestCase):
    def test_the_agent_files_box_stays_last_in_the_turn(self) -> None:
        script = (_STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("function renderAgentFiles", script)
        self.assertIn("el.lastElementChild !== agentFilesBox", script)
        self.assertIn("view.agentFiles(m.media)", script)


class VoiceEditAndMeterStaticTests(unittest.TestCase):
    def test_edit_form_lists_recordings_and_the_recorder_shows_a_level_meter(self) -> None:
        script = (_STATIC / "app.js").read_text(encoding="utf-8")
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        style = (_STATIC / "style.css").read_text(encoding="utf-8")
        self.assertIn("msg-edit-recording", script)
        self.assertIn("recordings", script)
        self.assertIn("createAnalyser", script)
        self.assertIn('id="mic-level"', html)
        self.assertIn(".mic-level", style)
        self.assertNotIn("font-size: .85em; white-space: pre-wrap", style)  # the heard text is normal-sized
