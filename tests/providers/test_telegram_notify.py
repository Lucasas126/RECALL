import unittest
from unittest.mock import patch

from src.providers.telegram_notify import TelegramNotifier, link_entities


class _Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class LinkEntitiesTest(unittest.TestCase):
    def test_url_becomes_text_link_with_utf16_offsets(self):
        link = "http://localhost:8766/#agents/aria/dreams/20261103-134033-d40d"
        text = f"Sesión → Aria 🌙\nÁbrela: {link}"
        [entity] = link_entities(text)
        self.assertEqual(entity["type"], "text_link")
        self.assertEqual(entity["url"], link)
        # "🌙" is two UTF-16 code units, so the offset is one more than the Python index
        self.assertEqual(entity["offset"], text.index("http") + 1)
        self.assertEqual(entity["length"], len(link))

    def test_no_urls_no_entities(self):
        self.assertEqual(link_entities("Dream session completed for Aria."), [])


class NotifierSendTest(unittest.TestCase):
    def test_sends_entities_and_falls_back_to_plain_text_on_400(self):
        notifier = TelegramNotifier("token", {123: "user-a"})
        calls = []

        def fake_post(url, json, timeout):
            calls.append(json)
            return _Response(400 if "entities" in json else 200)

        with patch("src.providers.telegram_notify.httpx.post", side_effect=fake_post):
            sent = notifier.send("user-a", "Open it: http://localhost:8766/#agents/aria/dreams/x")
        self.assertEqual(sent, 1)
        self.assertIn("entities", calls[0])
        self.assertNotIn("entities", calls[1])

    def test_send_markdown_sends_converted_entities_and_falls_back_on_400(self):
        notifier = TelegramNotifier("token", {123: "user-a"})
        calls = []

        def fake_post(url, json, timeout):
            calls.append(json)
            return _Response(400 if "entities" in json else 200)

        with patch("src.providers.telegram_notify.httpx.post", side_effect=fake_post):
            sent = notifier.send_markdown("user-a", "**✅ Done**\n\n> quote")
        self.assertEqual(sent, 1)
        self.assertEqual(calls[0]["text"], "✅ Done\n\nquote")
        self.assertEqual(calls[0]["entities"][0]["type"], "bold")
        self.assertEqual(calls[1], {"chat_id": 123, "text": "✅ Done\n\nquote"})


if __name__ == "__main__":
    unittest.main()
