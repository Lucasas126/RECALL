import unittest
from unittest.mock import patch

import httpx

from src.providers.ollama import OllamaProvider


def _error(text="registry.ollama.ai/library/llama3 does not support thinking"):
    request = httpx.Request("POST", "http://ollama/api/chat")
    return httpx.HTTPStatusError("bad", request=request, response=httpx.Response(400, text=text, request=request))


class OllamaThinkFallbackTests(unittest.TestCase):
    def test_sync_retries_without_think_when_model_cannot_think(self):
        calls = []

        def post(url, payload, **kwargs):
            calls.append(payload)
            if "think" in payload:
                raise _error()
            return {"message": {"content": "ok"}}

        with patch("src.providers.ollama.post_chat", post):
            result = OllamaProvider(base_url="http://ollama").invoke([], "llama3", stream=False, reasoning=True)
        self.assertEqual(result["response"], "ok")
        self.assertIn("think", calls[0])
        self.assertNotIn("think", calls[1])

    def test_stream_retries_without_think_and_other_errors_propagate(self):
        payloads = []

        def stream(url, payload, **kwargs):
            payloads.append(payload)
            if "think" in payload:
                raise _error()
            return (line for line in ['{"message": {"content": "hi"}, "done": true}'])

        with patch("src.providers.ollama.stream_chat", stream):
            items = list(OllamaProvider(base_url="http://ollama").invoke([], "llama3", reasoning=True))
        self.assertEqual(len(payloads), 2)
        self.assertNotIn("think", payloads[1])
        self.assertEqual(items[0], ("content", "hi"))

        with patch("src.providers.ollama.post_chat", lambda *a, **k: (_ for _ in ()).throw(_error("boom"))):
            with self.assertRaises(httpx.HTTPStatusError):
                OllamaProvider(base_url="http://ollama").invoke([], "llama3", stream=False, reasoning=True)


if __name__ == "__main__":
    unittest.main()
