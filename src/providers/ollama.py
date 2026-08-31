import json
import os

import requests

from src.providers.provider import LLMProvider, format_completion
from src.utils.logger import get_logger

from typing import ClassVar

logger = get_logger("ollama")

DEFAULT_HOST = "http://localhost:11434"


class OllamaProvider(LLMProvider):

    name: ClassVar[str] = "ollama"

    # local ollama needs no key; base_url points at the ollama server
    api_key: str = ""
    base_url: str = ""

    def __init__(self, **data):
        super().__init__(**data)
        if not self.base_url:
            self.base_url = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None):
        """
        Invokes the Ollama API with the given messages and model.
        If stream is True, yields (kind, text) tuples then a final dict with usage info.
        If stream is False, returns a dict directly.
        """
        logger.debug("request model=%s stream=%s reasoning=%s msgs=%d", model, stream, reasoning, len(messages))
        if stream:
            return self.invoke_stream(messages, model, reasoning=reasoning)
        return self.invoke_sync(messages, model, reasoning=reasoning)

    def invoke_stream(self, messages: list[dict], model: str, reasoning=False):
        """Streaming variant — yields (kind, text) tuples then a final dict with usage info.

        kind is either "reasoning" (thinking tokens) or "content" (the actual answer).
        """
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": messages, "stream": True}
        if reasoning:
            payload["think"] = True
        logger.debug("ollama streaming request; payload=%s", payload)

        try:
            response_text = ""
            reasoning_text = ""
            usage_info = None
            with requests.post(url, json=payload, stream=True) as r:
                r.raise_for_status()
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    try:
                        data_obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    message = data_obj.get("message", {})
                    thinking = message.get("thinking")
                    if thinking:
                        reasoning_text += thinking
                        yield ("reasoning", thinking)

                    content = message.get("content")
                    if content:
                        response_text += content
                        yield ("content", content)

                    if data_obj.get("done"):
                        usage_info = {
                            "prompt_tokens": data_obj.get("prompt_eval_count"),
                            "completion_tokens": data_obj.get("eval_count"),
                        }

            logger.debug(format_completion("streaming", reasoning_text, response_text, usage_info, model))
            yield {"reasoning": reasoning_text, "response": response_text, "usage": usage_info, "model": model}

        except requests.RequestException as e:
            logger.error(f"Error during streaming from Ollama API: {e}")
            raise

    def invoke_sync(self, messages: list[dict], model: str, reasoning=False) -> dict:
        """Non-streaming variant — returns a dict with reasoning, response, usage and model."""
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": messages, "stream": False}
        if reasoning:
            payload["think"] = True
        logger.debug("ollama non-streaming request; payload=%s", payload)
        try:
            response = requests.post(url, json=payload)
            response.raise_for_status()
            data_obj = response.json()
            message = data_obj.get("message", {})
            usage_info = {
                "prompt_tokens": data_obj.get("prompt_eval_count"),
                "completion_tokens": data_obj.get("eval_count"),
            }
            logger.debug(format_completion("non-streaming", message.get("thinking", ""), message.get("content", ""), usage_info, model))
            return {
                "reasoning": message.get("thinking", ""),
                "response": message.get("content", ""),
                "usage": usage_info,
                "model": model,
            }
        except requests.RequestException as e:
            logger.error(f"Error invoking Ollama API (non-streaming): {e}")
            raise

    def invoke_embeddings(self, input: list[str], model: str) -> list[list[float]]:
        payload = {"model": model, "input": list(input)}
        logger.debug("ollama embeddings request; payload=%s", payload)
        response = requests.post(f"{self.base_url}/api/embed", json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["embeddings"]
