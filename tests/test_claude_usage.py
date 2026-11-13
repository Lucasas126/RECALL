import json
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from src.providers.claude_subscription import usage


def _login(folder: str, *, expires_in: float = 3600, token: str = "secret-token") -> None:
    data = {"claudeAiOauth": {"accessToken": token, "expiresAt": (time.time() + expires_in) * 1000}}
    (Path(folder) / ".credentials.json").write_text(json.dumps(data), encoding="utf-8")


class _Response:
    def __init__(self, status: int, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class ParseUsageTests(unittest.TestCase):
    def test_keeps_existing_windows_and_clamps(self):
        rows = usage.parse_usage({
            "five_hour": {"utilization": 6.0, "resets_at": "2026-11-13T12:29:59+00:00"},
            "seven_day": {"utilization": 140, "resets_at": None},
            "seven_day_opus": None, "seven_day_sonnet": {"utilization": "x"},
        })
        self.assertEqual([r["key"] for r in rows], ["session", "week"])
        self.assertEqual(rows[1]["percent"], 100.0)
        self.assertIsNone(rows[1]["resets_at"])


class FetchUsageTests(unittest.TestCase):
    def test_no_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(usage.fetch_usage(folder), {"available": False, "reason": "no_credentials"})

    def test_expired_token_is_not_sent(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(usage.httpx, "get") as get:
            _login(folder, expires_in=-10)
            self.assertEqual(usage.fetch_usage(folder), {"available": False, "reason": "expired"})
            get.assert_not_called()

    def test_ok_never_returns_the_token(self):
        body = {"five_hour": {"utilization": 6.0, "resets_at": "x"}, "seven_day": {"utilization": 14.0, "resets_at": "y"}}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(usage.httpx, "get", return_value=_Response(200, body)) as get:
            _login(folder)
            result = usage.fetch_usage(folder)
        self.assertTrue(result["available"])
        self.assertEqual([w["percent"] for w in result["windows"]], [6.0, 14.0])
        self.assertNotIn("secret-token", json.dumps(result))
        self.assertEqual(get.call_args.args[0], usage.USAGE_URL)

    def test_http_errors(self):
        for status, reason in ((401, "unauthorized"), (500, "unreachable")):
            with tempfile.TemporaryDirectory() as folder, mock.patch.object(usage.httpx, "get", return_value=_Response(status, {})):
                _login(folder)
                self.assertEqual(usage.fetch_usage(folder)["reason"], reason)

    def test_network_error_and_bad_json(self):
        with tempfile.TemporaryDirectory() as folder:
            _login(folder)
            with mock.patch.object(usage.httpx, "get", side_effect=usage.httpx.ConnectError("x")):
                self.assertEqual(usage.fetch_usage(folder)["reason"], "unreachable")
            with mock.patch.object(usage.httpx, "get", return_value=_Response(200, ValueError())):
                self.assertEqual(usage.fetch_usage(folder)["reason"], "unreachable")


class ResetInTests(unittest.TestCase):
    NOW = datetime(2026, 11, 13, 10, 0, tzinfo=timezone.utc)

    def test_wording(self):
        for iso, expected in (
            ("2026-11-13T10:45:00+00:00", "45m"), ("2026-11-13T12:14:00+00:00", "2h 14m"),
            ("2026-11-16T15:00:00+00:00", "3d 5h"), ("2026-11-13T09:00:00+00:00", "0m"),
        ):
            self.assertEqual(usage.reset_in(iso, self.NOW), expected)

    def test_unknown(self):
        self.assertIsNone(usage.reset_in(None, self.NOW))
        self.assertIsNone(usage.reset_in("not a date", self.NOW))


if __name__ == "__main__":
    unittest.main()
