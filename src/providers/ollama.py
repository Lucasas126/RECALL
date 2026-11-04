import json
import os
from contextlib import closing

import requests
import httpx

from src.providers.provider import LLMProvider, format_completion
from src.providers.cancellation import CancellationToken
from src.providers.transport import post_chat, stream_chat
from src.utils.logger import get_logger

from typing import ClassVar

logger = get_logger("provider-ollama")

DEFAULT_HOST = "http://localhost:11434"


class OllamaUnavailable(RuntimeError):
    """The Ollama server did not answer or answered with an unexpected shape."""


def ollama_host(host: str | None = None) -> str:
    """The configured host, else OLLAMA_HOST, else the local default."""
    return (host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")


def is_ollama_cloud(model: str) -> bool:
    """Ollama cloud models (`name:cloud`, `name:tag-cloud`) run on Ollama's servers, not on the local machine."""
    return model.removeprefix("ollama:").rsplit(":", 1)[-1].endswith("cloud")


def list_ollama_models(host: str | None = None, *, timeout: float = 5.0) -> list[str]:
    """Names of the models installed on the Ollama server (`/api/tags`)."""
    url = f"{ollama_host(host)}/api/tags"
    try:
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        models = response.json().get("models")
    except (requests.RequestException, ValueError) as exc:
        raise OllamaUnavailable(f"Ollama at {ollama_host(host)} did not answer: {type(exc).__name__}") from exc
    if not isinstance(models, list):
        raise OllamaUnavailable(f"Ollama at {ollama_host(host)} returned an unexpected model list")
    return sorted({m["name"] for m in models if isinstance(m, dict) and isinstance(m.get("name"), str)})


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
            self.base_url = ollama_host()

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None, *, zdr: bool = False):
        """
        Invokes the Ollama API with the given messages and model.
        If stream is True, yields (kind, text) tuples then a final dict with usage info.
        If stream is False, returns a dict directly.
        zdr has no effect on local Ollama requests.
        """
        model = self.own_model(model)
        logger.debug("request model=%s stream=%s reasoning=%s msgs=%d", model, stream, reasoning, len(messages))
        if stream:
            return self.invoke_stream(messages, model, reasoning=reasoning, reasoning_effort=reasoning_effort, tools=tools, cancellation=cancellation)
        return self.invoke_sync(messages, model, reasoning=reasoning, reasoning_effort=reasoning_effort, tools=tools, cancellation=cancellation)

    def invoke_stream(self, messages: list[dict], model: str, reasoning=False, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None):
        """Streaming variant — yields (kind, text) tuples then a final dict with usage info.

        kind is either "reasoning" (thinking tokens) or "content" (the actual answer).
        """
        model = self.own_model(model)
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
            with closing(stream_chat(url, payload, cancellation=cancellation)) as lines:
                for line in lines:
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

        except httpx.HTTPError as e:
            logger.error(f"Error during streaming from Ollama API: {e}")
            raise

    def invoke_sync(self, messages: list[dict], model: str, reasoning=False, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None) -> dict:
        """Non-streaming variant — returns a dict with reasoning, response, usage and model."""
        model = self.own_model(model)
        url = f"{self.base_url}/api/chat"
        payload = {"model": model, "messages": _to_ollama_messages(messages), "stream": False}
        if reasoning:
            payload["think"] = reasoning_effort or True
        if tools:
            payload["tools"] = tools
        logger.debug("ollama non-streaming request; payload=%s", payload)
        try:
            data_obj = post_chat(url, payload, cancellation=cancellation)
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
        except httpx.HTTPError as e:
            logger.error(f"Error invoking Ollama API (non-streaming): {e}")
            raise

    def invoke_embeddings(self, input: list[str], model: str) -> list[list[float]]:
        model = self.own_model(model)
        payload = {"model": model, "input": list(input)}
        logger.debug("ollama embeddings request; payload=%s", payload)
        response = requests.post(f"{self.base_url}/api/embed", json=payload, timeout=60)
        response.raise_for_status()
        return response.json()["embeddings"]

    def context_window(self, model: str):
        """Return the model's context length from ollama's /api/show, or None if unknown."""
        model = self.own_model(model)
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

    def supported_modalities(self, model: str) -> set[str]:
        """Read Ollama's declared model capabilities without guessing unsupported inputs."""
        model = self.own_model(model)
        response = requests.post(f"{self.base_url}/api/show", json={"model": model}, timeout=30)
        response.raise_for_status()
        capabilities = response.json().get("capabilities", [])
        return {"image"} if "vision" in capabilities else set()
