from __future__ import annotations

import unittest

from src.agent.agent import Agent
from src.core.index import _indexable_content
from src.providers.provider import unsupported_modality_message
from src.service.chat import ChatAttachment


class MultimodalTests(unittest.TestCase):
    def test_attachment_validation_accepts_supported_shape(self) -> None:
        attachment = ChatAttachment(
            modality="image",
            name="photo.png",
            mime_type="image/png",
            format="png",
            data_url="data:image/png;base64," + ("a" * 32),
        )
        self.assertEqual(attachment.modality, "image")

    def test_content_parts_use_provider_formats(self) -> None:
        content = Agent._user_content(
            "Describe these",
            [
                {
                    "modality": "image",
                    "data_url": "data:image/png;base64,abc",
                    "format": "png",
                },
                {
                    "modality": "audio",
                    "data_url": "data:audio/mpeg;base64,xyz",
                    "format": "mpeg",
                },
                {
                    "modality": "video",
                    "data_url": "data:video/mp4;base64,123",
                    "format": "mp4",
                },
            ],
        )
        self.assertEqual(content[0], {"type": "text", "text": "[VIDEO] Describe these"})
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[2]["input_audio"]["data"], "xyz")
        self.assertEqual(content[3]["type"], "video_url")

    def test_unsupported_message_uses_user_language(self) -> None:
        self.assertEqual(
            unsupported_modality_message("es-ES", "video"),
            "Este modelo no admite entradas de vídeo.",
        )

    def test_indexable_content_excludes_attachment_payloads(self) -> None:
        content = _indexable_content([
            {"type": "text", "text": "Describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,secret"}},
            {"type": "input_audio", "input_audio": {"data": "secret"}},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,secret"}},
        ])
        self.assertEqual(content, "Describe this [image_url] [input_audio] [video_url]")
        self.assertNotIn("secret", content)


if __name__ == "__main__":
    unittest.main()
