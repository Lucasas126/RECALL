import os
import requests
import json
from contextlib import closing
from typing import ClassVar

import httpx

from src.config.models import PromptCacheConfig
from src.config.settings import PROMPT_CACHE
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.provider import LLMProvider, format_completion
from src.providers.catalog import list_openrouter_models
from src.providers.catalog import list_openrouter_transcription_models
from src.providers.transport import post_chat, stream_chat
from src.utils.i18n import UserMessageError
from src.utils.logger import get_logger

import dotenv

dotenv.load_dotenv()
logger = get_logger("provider-openrouter")

# cache of model id -> context length, fetched once from the models endpoint
_MODEL_CONTEXT: dict[str, int] = {}
_MODEL_CONTEXT_LOADED = False


class OpenRouterRateLimited(UserMessageError, RuntimeError):
    """The model's upstream provider is temporarily rate-limited (HTTP 429)."""


def _raise_openrouter_error(status_code, body) -> None:
    """Raise a clear error carrying OpenRouter's own message and upstream detail (never swallow it)."""
    try:
        error = json.loads(body)["error"] if isinstance(body, str) else body
        message = error["message"]
    except Exception:
        error, message = {}, str(body)[:300]
    metadata = error.get("metadata") if isinstance(error.get("metadata"), dict) else {}
    # "Provider returned error" hides the real cause, which OpenRouter puts in metadata.raw
    detail = str(metadata.get("raw") or "")[:300]
    provider = metadata.get("provider_name")
    if str(status_code or error.get("code")) == "429":
        suffix = f" ({provider})" if provider else ""
        raise OpenRouterRateLimited(
            "The model's provider is temporarily rate-limited{suffix}: {detail} "
            "Retry shortly or choose another model.",
            suffix=suffix, detail=detail or message,
        )
    if detail and detail not in message:
        message = f"{message}: {detail}"
    raise RuntimeError(f"openrouter API error {status_code}: {message}")


def _reasoning_payload(effort, tokens) -> dict:
    """Build the openrouter `reasoning` config: effort takes priority over a token budget."""
    if effort:
        return {"effort": effort}
    if tokens:
        return {"max_tokens": tokens}
    return {}


_CACHE_BREAKPOINT = {"type": "ephemeral"}


def is_anthropic_model(model: str) -> bool:
    """True for Claude models on OpenRouter, including `~anthropic/...-latest` aliases."""
    return model.removeprefix("~").startswith("anthropic/")


def _with_breakpoint(message: dict) -> dict | None:
    """Copy of the message with a cache breakpoint on its last text block, or None when it has no text to mark."""
    content = message.get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content}]
    elif isinstance(content, list):
        blocks = [dict(block) if isinstance(block, dict) else block for block in content]
    else:
        return None
    for index in range(len(blocks) - 1, -1, -1):
        block = blocks[index]
        if isinstance(block, dict) and block.get("type") == "text" and str(block.get("text", "")).strip():
            blocks[index] = {**block, "cache_control": _CACHE_BREAKPOINT}
            return {**message, "content": blocks}
    return None


def with_prompt_cache(messages: list[dict]) -> list[dict]:
    """Copy of the messages with Anthropic cache breakpoints on the system prompt and the last markable message.

    Anthropic caches everything before a breakpoint and, on the next call, reuses the longest prefix it finds
    within the previous 20 blocks, so the second breakpoint moves forward with the conversation.
    """
    marked = list(messages)
    system_index = next((i for i, m in enumerate(marked) if m.get("role") == "system"), None)
    if system_index is not None and (system := _with_breakpoint(marked[system_index])) is not None:
        marked[system_index] = system
    for index in range(len(marked) - 1, -1, -1):
        if marked[index].get("role") == "system":
            continue
        if (message := _with_breakpoint(marked[index])) is not None:
            marked[index] = message
            break
    return marked


def _normalize_tool_calls(tool_calls):
    """Map OpenAI-style tool_calls to the neutral {id, name, arguments} shape."""
    if not tool_calls:
        return None
    out = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        out.append({"id": tc.get("id", ""), "name": fn.get("name", ""), "arguments": fn.get("arguments", "")})
    return out or None


# DeepSeek models (e.g. R1) leak tool calls as inline special tokens in the
# streamed content instead of standard tool_calls deltas. These mark them.
_DS_BEGIN = "<｜tool▁calls▁begin｜>"
_DS_CALL = "<｜tool▁call▁begin｜>"
_DS_SEP = "<｜tool▁sep｜>"
_DS_PREFIX = "<｜tool"  # any deepseek tool marker starts with this


def _parse_deepseek_tool_calls(text: str):
    """Split DeepSeek inline tool-call markers out of streamed content.

    Returns (clean_text, tool_calls) where tool_calls is the neutral shape or None.
    """
    if _DS_BEGIN not in text:
        return text, None
    clean, block = text.split(_DS_BEGIN, 1)
    calls = []
    for part in block.split(_DS_CALL)[1:]:
        if _DS_SEP not in part:
            continue
        after = part.split(_DS_SEP, 1)[1]
        name = after.split("\n", 1)[0].split("```", 1)[0].strip()
        args = ""
        if "```" in after:
            inner = after.split("```", 1)[1]
            if inner.startswith("json"):
                inner = inner[4:]
            args = inner.split("```", 1)[0].strip()
        if name:
            calls.append({"id": f"call_{len(calls)}", "name": name, "arguments": args})
    return clean.rstrip(), (calls or None)


class OpenRouterProvider(LLMProvider):

    name: ClassVar[str] = "openrouter"

    api_key: str = ""
    prompt_cache: PromptCacheConfig = PROMPT_CACHE

    def __init__(self, **data):
        super().__init__(**data)
        # self.api_key = os.environ.get("OPENROUTER_API_KEY", "")

    def _chat_request(self, model: str, messages: list[dict], zdr: bool) -> tuple[list[dict], dict]:
        """Messages and OpenRouter routing preferences for one chat request; Claude gets cache breakpoints."""
        routing: dict = {"zdr": zdr}
        if self.prompt_cache.enabled and is_anthropic_model(model):
            messages = with_prompt_cache(messages)
            if self.prompt_cache.anthropic_providers:
                routing["order"] = list(self.prompt_cache.anthropic_providers)
        return messages, routing

    def supported_modalities(self, model: str) -> set[str]:
        """Read the selected model's declared input modalities from OpenRouter."""
        model = self.own_model(model)
        matches = list_openrouter_models(model, zdr=False)
        selected = next((item for item in matches if item.id == model), None)
        return set(selected.input_modalities) if selected else set()

    def transcribe(
        self, audio_data: str, audio_format: str, *,
        model: str, language: str | None = None, zdr: bool = True,
    ) -> str:
        """Transcribe base64 audio through OpenRouter's dedicated STT endpoint."""
        model = self.own_model(model)
        if not any(item.id == model for item in list_openrouter_transcription_models("", zdr=zdr)):
            raise RuntimeError(
                f"speech-to-text model '{model}' is not available with the selected ZDR policy"
            )
        payload = {
            "model": model,
            "input_audio": {"data": audio_data, "format": audio_format},
            "provider": {"zdr": zdr},
        }
        if language:
            payload["language"] = language.split("-", 1)[0].lower()
        response = requests.post(
            "https://openrouter.ai/api/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        if not response.ok:
            _raise_openrouter_error(response.status_code, response.text)
        text = response.json().get("text")
        if not isinstance(text, str):
            raise RuntimeError("OpenRouter speech-to-text returned no transcription text")
        return text.strip()

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None, *, zdr: bool = True):
        """
        Invokes the OpenRouter API with the given messages and model.
        If stream is True, yields (kind, text) tuples then a final dict with usage info.
        If stream is False, returns a dict directly.
        """
        model = self.own_model(model)
        logger.debug("request model=%s stream=%s reasoning=%s msgs=%d", model, stream, reasoning, len(messages))
        if stream:
            return self.invoke_stream(messages, model, self.api_key, reasoning=reasoning, reasoning_tokens=reasoning_tokens, reasoning_effort=reasoning_effort, tools=tools, cancellation=cancellation, zdr=zdr)
        return self.invoke_sync(messages, model, self.api_key, reasoning=reasoning, reasoning_tokens=reasoning_tokens, reasoning_effort=reasoning_effort, tools=tools, cancellation=cancellation, zdr=zdr)

    def invoke_stream(self, messages: list[dict], model: str, api_key: str, reasoning=False, reasoning_tokens=None, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None, *, zdr: bool = True):
        """Streaming variant — yields (kind, text) tuples then a final dict with usage info.

        kind is either "reasoning" (thinking tokens) or "content" (the actual answer).
        """
        model = self.own_model(model)

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        messages, routing = self._chat_request(model, messages, zdr)
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "provider": routing,
        }
        logger.debug("openrouter streaming request; payload=%s", payload)
        if reasoning:
            payload["reasoning"] = _reasoning_payload(reasoning_effort, reasoning_tokens)
        if tools:
            payload["tools"] = tools

        try:
            response_text = ""
            reasoning_text = ""
            usage_info = None
            tool_calls_acc: dict[int, dict] = {}
            emitted = 0  # chars of response_text already yielded to the caller
            ds_started = False  # a deepseek inline tool-call marker has appeared
            with closing(stream_chat(url, payload, headers=headers, cancellation=cancellation)) as lines:
                for line in lines:
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data == "[DONE]":
                        break
                    try:
                        data_obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if data_obj.get("error"):
                        _raise_openrouter_error(data_obj.get("code", ""), data_obj["error"])
                    if "usage" in data_obj:
                        usage_info = data_obj["usage"]
                    choices = data_obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    reasoning_chunk = delta.get("reasoning")
                    if reasoning_chunk:
                        reasoning_text += reasoning_chunk
                        yield ("reasoning", reasoning_chunk)
                    content = delta.get("content")
                    if content:
                        response_text += content
                        if not ds_started:
                            pos = response_text.find(_DS_PREFIX)
                            if pos == -1:
                                safe = len(response_text) - len(_DS_PREFIX)
                                if safe > emitted:
                                    yield ("content", response_text[emitted:safe])
                                    emitted = safe
                            else:
                                if pos > emitted:
                                    yield ("content", response_text[emitted:pos])
                                emitted = pos
                                ds_started = True
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

            # Yield the final result with usage info after streaming is complete
            if not ds_started and emitted < len(response_text):
                yield ("content", response_text[emitted:])  # flush the guarded tail
            clean_text, ds_tool_calls = _parse_deepseek_tool_calls(response_text)
            tool_calls = [tc for tc in tool_calls_acc.values() if tc["name"]] or ds_tool_calls or None
            final_response = clean_text if ds_tool_calls else response_text
            logger.debug(format_completion("streaming", reasoning_text, final_response, usage_info, model))
            yield {"reasoning": reasoning_text, "response": final_response, "usage": usage_info, "model": model, "tool_calls": tool_calls}

        except httpx.HTTPStatusError as e:
            _raise_openrouter_error(e.response.status_code, e.response.text)
        except httpx.RequestError as e:
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            if response_text or reasoning_text or tool_calls_acc:
                logger.error("OpenRouter stream failed after partial output: %s", e)
                raise
            logger.warning(f"Error during streaming from OpenRouter API: {e}, retrying without streaming")
            try:
                response_data = post_chat(url, {**payload, "stream": False}, headers=headers, cancellation=cancellation)
                msg = response_data["choices"][0]["message"]
                if msg.get("reasoning"):
                    yield ("reasoning", msg["reasoning"])
                if msg.get("content"):
                    yield ("content", msg["content"])
                yield {
                    "reasoning": msg.get("reasoning", ""),
                    "response": msg.get("content", ""),
                    "usage": response_data.get("usage"),
                    "model": model,
                    "tool_calls": _normalize_tool_calls(msg.get("tool_calls")),
                }
            except GenerationCancelled:
                raise
            except Exception as retry_error:
                logger.error(f"Fallback to non-streaming also failed: {retry_error}")
                raise
        except GenerationCancelled:
            raise
        except Exception as e:
            logger.error(f"Unexpected error during streaming: {e}")
            raise

    def invoke_sync(self, messages: list[dict], model: str, api_key: str, reasoning=False, reasoning_tokens=None, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None, *, zdr: bool = True) -> dict:
        """Non-streaming variant — returns a dict with reasoning, response, usage and model."""
        model = self.own_model(model)
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        messages, routing = self._chat_request(model, messages, zdr)
        payload = {"model": model, "messages": messages, "stream": False, "provider": routing}
        logger.debug("openrouter non-streaming request; payload=%s", payload)
        if reasoning:
            payload["reasoning"] = _reasoning_payload(reasoning_effort, reasoning_tokens)
        if tools:
            payload["tools"] = tools
        try:
            response_data = post_chat(url, payload, headers=headers, cancellation=cancellation)
            msg = response_data["choices"][0]["message"]
            logger.debug(format_completion("non-streaming", msg.get("reasoning", ""), msg.get("content", ""), response_data.get("usage"), model))
            return {
                "reasoning": msg.get("reasoning", ""),
                "response": msg.get("content", ""),
                "usage": response_data.get("usage"),
                "model": model,
                "tool_calls": _normalize_tool_calls(msg.get("tool_calls")),
            }
        except httpx.HTTPStatusError as e:
            _raise_openrouter_error(e.response.status_code, e.response.text)
        except httpx.RequestError as e:
            logger.error(f"Error invoking OpenRouter API (non-streaming): {e}")
            raise
        except (KeyError, IndexError) as e:
            logger.error(f"Unexpected response format from OpenRouter API: {e}")
            raise
        except GenerationCancelled:
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise

    def invoke_embeddings(self, input: list[str], model: str) -> list[list[float]]:
        model = self.own_model(model)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {"model": model, "input": list(input)}
        logger.debug("openrouter embeddings request; payload=%s", payload)
        response = requests.post(
            "https://openrouter.ai/api/v1/embeddings",
            json=payload,
            headers=headers,
            timeout=60,
        )
        logger.debug("openrouter embeddings response; status_code=%s response_text=%s", response.status_code, response.text)
        response.raise_for_status()
        data = response.json()["data"]
        # data is a list of {"index": int, "embedding": list[float]}
        data.sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data]

    def context_window(self, model: str):
        """Return the model's max context length from OpenRouter's models list (cached)."""
        model = self.own_model(model)
        global _MODEL_CONTEXT_LOADED
        if not _MODEL_CONTEXT_LOADED:
            try:
                r = requests.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=30,
                )
                r.raise_for_status()
                for m in r.json().get("data", []):
                    if m.get("context_length"):
                        _MODEL_CONTEXT[m["id"]] = m["context_length"]
                _MODEL_CONTEXT_LOADED = True
            except Exception as e:
                logger.warning("could not fetch model contexts: %s", e)
                return None
        return _MODEL_CONTEXT.get(model)

    def reasoning_options(self, model: str) -> dict | None:
        """Return a model's reasoning capabilities (supported_efforts, default_effort, ...) or None."""
        model = self.own_model(model)
        try:
            r = requests.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=30,
            )
            r.raise_for_status()
            for m in r.json().get("data", []):
                if m.get("id") == model:
                    return m.get("reasoning")
        except Exception as e:
            logger.warning("could not fetch reasoning options: %s", e)
        return None