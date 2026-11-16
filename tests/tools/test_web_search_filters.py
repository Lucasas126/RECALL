from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from src.agent.toolsets.web import build_web_tools
from src.config.models import TavilyConfig
from src.providers.tavily import TavilyClient


def _hit(url: str, published_date: str | None) -> dict:
    return {"title": "t", "url": url, "content": "excerpt", "published_date": published_date}


class WebSearchFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TavilyClient("key", TavilyConfig())
        self.post = patch.object(self.client, "_post", return_value={"results": []}).start()
        self.addCleanup(patch.stopall)

    def test_filters_are_sent_to_tavily(self) -> None:
        self.client.search(
            "release notes", 3, topic="news", start_date="2025-01-01", end_date="2025-06-30",
            include_domains=["NASA.gov"], exclude_domains=["example.com"],
        )
        endpoint, payload = self.post.call_args.args
        self.assertEqual(endpoint, "search")
        self.assertEqual(payload["topic"], "news")
        self.assertEqual(payload["start_date"], "2025-01-01")
        self.assertEqual(payload["end_date"], "2025-06-30")
        self.assertEqual(payload["include_domains"], ["nasa.gov"])
        self.assertEqual(payload["exclude_domains"], ["example.com"])
        self.assertTrue(payload["include_published_date"])
        self.assertNotIn("time_range", payload)
        self.assertNotIn("country", payload)

    def test_unset_filters_are_omitted(self) -> None:
        self.client.search("plain query")
        _, payload = self.post.call_args.args
        self.assertEqual(payload["topic"], "general")
        self.assertEqual(payload["search_depth"], "basic")
        for key in ("time_range", "start_date", "end_date", "country", "include_domains", "exclude_domains"):
            self.assertNotIn(key, payload)

    def test_invalid_filter_combinations_are_rejected(self) -> None:
        invalid_calls = [
            dict(time_range="week", start_date="2025-01-01"),
            dict(start_date="2025-02-01", end_date="2025-01-01"),
            dict(topic="finance", country="spain"),
            dict(include_domains=["https://example.com/path"]),
            dict(include_domains=["a.com"], exclude_domains=["a.com"]),
            dict(start_date="not-a-date"),
            dict(time_range="decade"),
        ]
        for kwargs in invalid_calls:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.client.search("query", **kwargs)
        self.post.assert_not_called()

    def test_tool_notes_undated_results_when_date_filter_is_used(self) -> None:
        self.post.return_value = {"results": [
            _hit("https://a.example.org/x", "2025-03-01"),
            _hit("https://b.example.org/y", None),
        ]}
        web_search = build_web_tools(self.client)[0]
        output = json.loads(web_search(query="q", time_range="month"))
        self.assertEqual(len(output["results"]), 2)
        self.assertIn("notes", output)
        self.assertIn("1 result(s) have no detectable publication date", output["notes"][0])

    def test_tool_adds_no_note_without_date_filter(self) -> None:
        self.post.return_value = {"results": [_hit("https://b.example.org/y", None)]}
        web_search = build_web_tools(self.client)[0]
        output = json.loads(web_search(query="q"))
        self.assertNotIn("notes", output)

    def test_tool_passes_empty_strings_as_unset(self) -> None:
        self.post.return_value = {"results": []}
        web_search = build_web_tools(self.client)[0]
        web_search(query="q", time_range="", start_date="", end_date="", include_domains=[], exclude_domains=None)
        _, payload = self.post.call_args.args
        self.assertNotIn("time_range", payload)
        self.assertNotIn("start_date", payload)
        self.assertNotIn("include_domains", payload)


if __name__ == "__main__":
    unittest.main()
