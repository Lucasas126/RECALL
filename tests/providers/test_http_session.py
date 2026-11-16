import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.providers import transport


class HttpSessionTests(unittest.TestCase):
    def test_same_thread_reuses_one_session(self) -> None:
        self.assertIs(transport.http_session(), transport.http_session())

    def test_other_thread_gets_its_own_session(self) -> None:
        seen = []
        thread = threading.Thread(target=lambda: seen.append(transport.http_session()))
        thread.start()
        thread.join()
        self.assertIsNot(seen[0], transport.http_session())

    def test_post_json_retries_once_when_the_connection_was_closed(self) -> None:
        ok = MagicMock()
        with patch.object(requests.Session, "post", side_effect=[requests.ConnectionError("closed"), ok]) as post:
            self.assertIs(transport.post_json("https://example.invalid", json={}), ok)
        self.assertEqual(post.call_count, 2)

    def test_post_json_does_not_hide_a_second_failure(self) -> None:
        with patch.object(requests.Session, "post", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(requests.ConnectionError):
                transport.post_json("https://example.invalid", json={})


if __name__ == "__main__":
    unittest.main()
