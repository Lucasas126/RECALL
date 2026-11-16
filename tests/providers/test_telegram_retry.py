from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from src.providers import telegram_notify
from src.providers.telegram_notify import TelegramNotifier


class _Response:
    def __init__(self, status_code: int = 200):
        self.status_code = status_code


def _connect_error() -> httpx.ConnectError:
    # httpx errors carry the request, whose URL holds the bot token
    return httpx.ConnectError("[Errno 104] Connection reset by peer", request=httpx.Request("POST", "https://api.telegram.org/bot-SECRET/sendDocument"))


class ConnectRetryTests(unittest.TestCase):
    """The connection to Telegram sometimes fails to open (`ConnectError`) and works a moment later."""

    def setUp(self) -> None:
        self.notifier = TelegramNotifier("SECRET", {123: "user-a"})
        patcher = patch.object(telegram_notify.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, outcomes):
        """Patch `httpx.post` to answer the given outcomes in order (an exception is raised, a status code is answered)."""
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            return _Response(outcome)

        return calls, patch("src.providers.telegram_notify.httpx.post", side_effect=fake)

    def test_a_file_is_sent_after_one_failed_connection(self) -> None:
        calls, patched = self.post([_connect_error(), 200])
        with patched:
            self.assertEqual(self.notifier.send_file("user-a", "a.pdf", b"x", "document", "hi"), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.sleep.call_count, 1)

    def test_a_message_is_sent_after_two_failed_connections(self) -> None:
        calls, patched = self.post([_connect_error(), httpx.ConnectTimeout("slow", request=httpx.Request("POST", "https://x")), 200])
        with patched:
            self.assertEqual(self.notifier.send("user-a", "hello"), 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [0.7, 1.4])  # the pause doubles

    def test_it_gives_up_after_the_retries_and_does_not_leak_the_token(self) -> None:
        calls, patched = self.post([_connect_error()] * 3)
        with patched:
            with self.assertRaises(RuntimeError) as caught:
                self.notifier.send_file("user-a", "a.pdf", b"x", "document")
        self.assertEqual(len(calls), 1 + telegram_notify.CONNECT_RETRIES)
        self.assertEqual(str(caught.exception), "Telegram sendDocument failed: ConnectError")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_an_error_after_the_request_went_out_is_never_retried(self) -> None:
        for error in (
            httpx.ReadTimeout("no answer", request=httpx.Request("POST", "https://x")),
            httpx.WriteTimeout("stalled", request=httpx.Request("POST", "https://x")),
            httpx.RemoteProtocolError("dropped", request=httpx.Request("POST", "https://x")),
        ):
            with self.subTest(error=type(error).__name__):
                calls, patched = self.post([error, 200])
                with patched, self.assertRaises(RuntimeError):
                    self.notifier.send_file("user-a", "a.pdf", b"x", "document")
                self.assertEqual(len(calls), 1, "a request that may have been delivered was sent twice")

    def test_an_answer_from_telegram_is_never_retried(self) -> None:
        for status in (400, 429, 500):
            with self.subTest(status=status):
                calls, patched = self.post([status, 200])
                with patched, self.assertRaises(RuntimeError):
                    self.notifier.send_file("user-a", "a.pdf", b"x", "document")
                self.assertEqual(len(calls), 1)

    def test_a_clean_send_does_not_wait(self) -> None:
        calls, patched = self.post([200])
        with patched:
            self.notifier.send("user-a", "hello")
        self.assertEqual((len(calls), self.sleep.call_count), (1, 0))

    def test_each_chat_gets_its_own_retries(self) -> None:
        notifier = TelegramNotifier("SECRET", {1: "user-a", 2: "user-a"})
        calls, patched = self.post([_connect_error(), 200, _connect_error(), 200])
        with patched:
            self.assertEqual(notifier.send_file("user-a", "a.pdf", b"x", "document"), 2)
        self.assertEqual(len(calls), 4)

    def test_a_photo_refused_is_still_retried_as_a_document(self) -> None:
        calls, patched = self.post([_connect_error(), 400, 200])
        with patched:
            self.assertEqual(self.notifier.send_file("user-a", "a.png", b"x", "image"), 1)
        self.assertEqual([url.rsplit("/", 1)[1] for url in calls], ["sendPhoto", "sendPhoto", "sendDocument"])


if __name__ == "__main__":
    unittest.main()
