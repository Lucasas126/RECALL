"""Installable web app: the service worker route and file, and the session cookie that keeps a signed-in phone signed in."""

from __future__ import annotations

import json
import os
import re
import time
import unittest
from base64 import b64decode, b64encode
from pathlib import Path

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

import itsdangerous
from fastapi.testclient import TestClient

from src.web import app as web_app
from src.web.app import create_app
from tests.support.auth_helpers import login
from tests.tasks import test_service_steering as steering

SW = Path(__file__).resolve().parents[2] / "src" / "web" / "sw.js"


class ServiceWorkerFileTests(unittest.TestCase):
    """Fails when someone removes a rule that keeps data out of the worker."""

    def setUp(self) -> None:
        self.source = SW.read_text(encoding="utf-8")

    def test_api_and_event_streams_are_never_handled(self) -> None:
        self.assertIn('"/api/"', self.source)
        self.assertIn("text/event-stream", self.source)

    def test_only_get_requests_are_handled(self) -> None:
        self.assertRegex(self.source, r'request\.method\s*!==\s*"GET"')

    def test_responses_with_a_cookie_are_not_cached(self) -> None:
        self.assertIn('headers.has("set-cookie")', self.source)

    def test_ranged_requests_pass_through(self) -> None:
        self.assertIn('headers.has("range")', self.source)

    def test_shell_lists_no_data_path(self) -> None:
        shell = re.search(r"const SHELL = \[(.*?)\];", self.source, re.S).group(1)
        for path in re.findall(r'"([^"]+)"', shell):
            self.assertFalse(path.startswith("/api"), path)
            self.assertTrue(path == "/" or path.startswith("/static/"), path)
        for path in re.findall(r'"(/static/[^"]+)"', shell):
            self.assertTrue((SW.parent / path.removeprefix("/")).is_file(), path)


class PwaFixture(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service), base_url="http://testserver")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)


class ServiceWorkerRouteTests(PwaFixture):
    def test_served_from_the_root_as_javascript_without_caching(self) -> None:
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("application/javascript"))
        self.assertEqual(response.headers["cache-control"], "no-cache")
        self.assertNotIn("set-cookie", response.headers)

    def test_cache_name_carries_the_running_version(self) -> None:
        body = self.client.get("/sw.js").text
        self.assertNotIn("__RECALL_VERSION__", body)
        self.assertIn(f'"{web_app.app_version()}', body)

    def test_an_unauthenticated_visit_can_fetch_it(self) -> None:
        self.assertEqual(self.client.get("/sw.js").status_code, 200)


class SessionCookieTests(PwaFixture):
    @staticmethod
    def _cookie_header(response) -> str:
        return next(v for k, v in response.headers.multi_items() if k == "set-cookie" and v.startswith("recall_session="))

    def test_cookie_is_lax_and_lasts_30_days(self) -> None:
        header = self._cookie_header(login(self.client, "user-one")).lower()
        self.assertIn("samesite=lax", header)
        self.assertIn("max-age=2592000", header)
        self.assertIn("httponly", header)
        self.assertNotIn("secure", header)  # plain-HTTP development keeps working

    def test_cookie_is_sent_when_arriving_from_an_outside_link(self) -> None:
        # `lax` keeps the cookie on a top-level GET navigation from another site; `strict` would not.
        login(self.client, "user-one")
        response = self.client.get("/api/me", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("samesite=strict", self._cookie_header(login(self.client, "user-one")).lower())

    def test_fresh_session_is_not_resigned_on_every_request(self) -> None:
        login(self.client, "user-one")
        response = self.client.get("/api/me")
        self.assertNotIn("recall_session=", response.headers.get("set-cookie", ""))

    def test_old_session_is_renewed_once_while_in_use(self) -> None:
        login(self.client, "user-one")
        stale = time.time() - web_app.SESSION_RENEW_AFTER - 60
        value = self.client.cookies.get("recall_session")
        data = itsdangerous.TimestampSigner(os.environ["RECALL_SESSION_SECRET"]).unsign(value.encode()).decode()
        session = json.loads(b64decode(data))
        session[web_app._SESSION_RENEWED_KEY] = int(stale)
        forged = itsdangerous.TimestampSigner(os.environ["RECALL_SESSION_SECRET"]).sign(b64encode(json.dumps(session).encode())).decode()
        self.client.cookies.clear()
        self.client.cookies.set("recall_session", forged)
        renewed = self.client.get("/api/me")
        self.assertEqual(renewed.status_code, 200)
        self.assertIn("max-age=2592000", self._cookie_header(renewed).lower())
        self.assertNotIn("recall_session=", self.client.get("/api/me").headers.get("set-cookie", ""))

    def test_logout_clears_the_cookie(self) -> None:
        login(self.client, "user-one")
        self.assertIn("1970", self._cookie_header(self.client.post("/api/logout")))


if __name__ == "__main__":
    unittest.main()
