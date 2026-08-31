import json
import os

import requests

from src.providers.provider import LLMProvider, format_completion
from src.utils.logger import get_logger

from typing import ClassVar

logger = get_logger("provider-ollama")

DEFAULT_HOST = "http://localhost:11434"


def _to_ollama_messages(messages: list[dict]) -> list[dict]:
    """Translate OpenAI-shaped messages (tool_calls/tool) to ollama's wire format."""
    out: list[dict] = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args.strip() else {}
                    except json.JSONDecodeError:
                        args = {}
                calls.append({"function": {"name": fn.get("name", ""), "arguments": args}})
            out.append({"role": "assistant", "content": m.get("content", ""), "tool_calls": calls})
        elif m.get("role") == "tool":
            out.append({"role": "tool", "content": m.get("content", ""), "tool_name": m.get("name", "")})
        else:
            out.append(m)
    return out


class OllamaProvider(LLMProvider):

    name: ClassVar[str] = "ollama"

    # local ollama needs no key; base_url points at the ollama server
    api_key: str = ""
    base_url: str = ""

    def __init__(self, **data):
        super().__init__(**data)
        if not self.base_url:
            self.base_url = os.environ.get("OLLAMA_HOST", DEFAULT_HOST)

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None, reasoning_effort=None, tools=None):
        """
        Invokes the Ollama API with the given messages and model.
        If stream is True, yields (kind, text) tuples then a final dict with usage info.
        If stream is False, returns a dict directly.
        """
        logger.debug("request model=%s stream=%s reasoning=%s msgs=%d", model, stream, reasoning, len(messages))
        if stream:
            return self.invoke_stream(messages, model, reasoning=reasoning, reasoning_effort=reasoning_effort, tools=tools)
        return self.invoke_sync(messages, model, reasoning=reasoning, reasoning_effort=reasoning_effort, tools=tools)

    def invoke_stream(self, messages: list[dict], model: str, reasoning=False, reasoning_effort=None, tools=None):
        """Streaming variant — yields (kind, text) tuples then a final dict with usage info.

        kind is either "reasoning" (thinking tokens) or "content" (the actual answer).
        """
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": _to_ollama_messages(messages), "stream": True}
        if reasoning:
            payload["think"] = reasoning_effort or True
        if tools:
            payload["tools"] = tools
        logger.debug("ollama streaming request; payload=%s", payload)

        try:
            response_text = ""
            reasoning_text = ""
            usage_info = None
            tool_calls: list[dict] = []
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

                    for tc in message.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        tool_calls.append(
                            {"id": f"call_{len(tool_calls)}", "name": fn.get("name", ""), "arguments": fn.get("arguments", {})}
                        )

                    if data_obj.get("done"):
                        usage_info = {
                            "prompt_tokens": data_obj.get("prompt_eval_count"),
                            "completion_tokens": data_obj.get("eval_count"),
                        }

            logger.debug(format_completion("streaming", reasoning_text, response_text, usage_info, model))
            yield {"reasoning": reasoning_text, "response": response_text, "usage": usage_info, "model": model, "tool_calls": tool_calls or None}

        except requests.RequestException as e:
            logger.error(f"Error during streaming from Ollama API: {e}")
            raise

    def invoke_sync(self, messages: list[dict], model: str, reasoning=False, reasoning_effort=None, tools=None) -> dict:
        """Non-streaming variant — returns a dict with reasoning, response, usage and model."""
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": _to_ollama_messages(messages), "stream": False}
        if reasoning:
            payload["think"] = reasoning_effort or True
        if tools:
            payload["tools"] = tools
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
            tool_calls = [
                {"id": f"call_{i}", "name": tc.get("function", {}).get("name", ""), "arguments": tc.get("function", {}).get("arguments", {})}
                for i, tc in enumerate(message.get("tool_calls") or [])
            ] or None
            logger.debug(format_completion("non-streaming", message.get("thinking", ""), message.get("content", ""), usage_info, model))
            return {
                "reasoning": message.get("thinking", ""),
                "response": message.get("content", ""),
                "usage": usage_info,
                "model": model,
                "tool_calls": tool_calls,
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

    def context_window(self, model: str):
        """Return the model's context length from ollama's /api/show, or None if unknown."""
        try:
            r = requests.post(f"{self.base_url}/api/show", json={"model": model}, timeout=30)
            r.raise_for_status()
            info = r.json().get("model_info", {})
            for key, value in info.items():
                if key.endswith(".context_length"):
                    return value
        except Exception as e:
            logger.warning("could not fetch ollama context: %s", e)
        return None
