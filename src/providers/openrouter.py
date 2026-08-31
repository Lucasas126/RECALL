import os
import requests
import json
from typing import ClassVar

from src.providers.provider import LLMProvider, format_completion
from src.utils.logger import get_logger

import dotenv

dotenv.load_dotenv()
logger = get_logger("provider-openrouter")

# cache of model id -> context length, fetched once from the models endpoint
_MODEL_CONTEXT: dict[str, int] = {}
_MODEL_CONTEXT_LOADED = False


def _raise_openrouter_error(status_code, body) -> None:
    """Raise a clear error carrying OpenRouter's own message (never swallow it)."""
    try:
        message = json.loads(body)["error"]["message"] if isinstance(body, str) else body["message"]
    except Exception:
        message = str(body)[:300]
    raise RuntimeError(f"openrouter API error {status_code}: {message}")


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

    def __init__(self, **data):
        super().__init__(**data)
        # self.api_key = os.environ.get("OPENROUTER_API_KEY", "")

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None, tools=None):
        """
        Invokes the OpenRouter API with the given messages and model.
        If stream is True, yields (kind, text) tuples then a final dict with usage info.
        If stream is False, returns a dict directly.
        """
        logger.debug("request model=%s stream=%s reasoning=%s msgs=%d", model, stream, reasoning, len(messages))
        if stream:
            return self.invoke_stream(messages, model, self.api_key, reasoning=reasoning, reasoning_tokens=reasoning_tokens, tools=tools)
        return self.invoke_sync(messages, model, self.api_key, reasoning=reasoning, reasoning_tokens=reasoning_tokens, tools=tools)

    def invoke_stream(self, messages: list[dict], model: str, api_key: str, reasoning=False, reasoning_tokens=None, tools=None):
        """Streaming variant — yields (kind, text) tuples then a final dict with usage info.

        kind is either "reasoning" (thinking tokens) or "content" (the actual answer).
        """

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
        }
        logger.debug("openrouter streaming request; payload=%s", payload)
        if reasoning:
            payload["reasoning"] = {"max_tokens": reasoning_tokens} if reasoning_tokens else {}
        if tools:
            payload["tools"] = tools

        try:
            response_text = ""
            reasoning_text = ""
            usage_info = None
            tool_calls_acc: dict[int, dict] = {}
            emitted = 0  # chars of response_text already yielded to the caller
            ds_started = False  # a deepseek inline tool-call marker has appeared
            buffer = ""
            with requests.post(url, headers=headers, json=payload, stream=True) as r:
                if r.status_code != 200:
                    _raise_openrouter_error(r.status_code, r.text)
                r.encoding = "utf-8"  # SSE defaults to latin-1; force utf-8 to avoid mojibake
                for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
                    buffer += chunk
                    while True:
                        try:
                            line_end = buffer.find('\n')
                            if line_end == -1:
                                break

                            line = buffer[:line_end].strip()
                            buffer = buffer[line_end + 1:]

                            # Skip SSE comments (lines starting with ":"), e.g. the
                            # ": OPENROUTER PROCESSING" keep-alive — they are not JSON
                            if line.startswith(':'):
                                continue

                            if line.startswith('data: '):
                                data = line[6:]
                                if data == '[DONE]':
                                    break

                                try:
                                    data_obj = json.loads(data)
                                    if data_obj.get("error"):
                                        _raise_openrouter_error(data_obj.get("code", ""), data_obj["error"])
                                    delta = data_obj["choices"][0]["delta"]

                                    reasoning_chunk = delta.get("reasoning")
                                    if reasoning_chunk:
                                        reasoning_text += reasoning_chunk
                                        yield ("reasoning", reasoning_chunk)

                                    content = delta.get("content")
                                    if content:
                                        response_text += content
                                        # hold back deepseek tool markers instead of leaking them
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

                                    # Capture usage info if present in the final message
                                    if "usage" in data_obj:
                                        usage_info = data_obj["usage"]
                                except json.JSONDecodeError:
                                    pass

                        except RuntimeError:
                            raise  # surfaced API error: never swallow
                        except Exception:
                            break

            # Yield the final result with usage info after streaming is complete
            if not ds_started and emitted < len(response_text):
                yield ("content", response_text[emitted:])  # flush the guarded tail
            clean_text, ds_tool_calls = _parse_deepseek_tool_calls(response_text)
            tool_calls = [tc for tc in tool_calls_acc.values() if tc["name"]] or ds_tool_calls or None
            final_response = clean_text if ds_tool_calls else response_text
            logger.debug(format_completion("streaming", reasoning_text, final_response, usage_info, model))
            yield {"reasoning": reasoning_text, "response": final_response, "usage": usage_info, "model": model, "tool_calls": tool_calls}

        except requests.RequestException as e:
            logger.warning(f"Error during streaming from OpenRouter API: {e}, retrying without streaming")
            try:
                response = requests.post(url, headers=headers, json={**payload, "stream": False})
                response.raise_for_status()
                response_data = response.json()
                msg = response_data["choices"][0]["message"]
                yield {
                    "reasoning": msg.get("reasoning", ""),
                    "response": msg.get("content", ""),
                    "usage": response_data.get("usage"),
                    "model": model,
                    "tool_calls": _normalize_tool_calls(msg.get("tool_calls")),
                }
            except Exception as retry_error:
                logger.error(f"Fallback to non-streaming also failed: {retry_error}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error during streaming: {e}")
            raise

    def invoke_sync(self, messages: list[dict], model: str, api_key: str, reasoning=False, reasoning_tokens=None, tools=None) -> dict:
        """Non-streaming variant — returns a dict with reasoning, response, usage and model."""
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {"model": model, "messages": messages, "stream": False}
        logger.debug("openrouter non-streaming request; payload=%s", payload)
        if reasoning:
            payload["reasoning"] = {"max_tokens": reasoning_tokens} if reasoning_tokens else {}
        if tools:
            payload["tools"] = tools
        try:
            response = requests.post(url, headers=headers, json=payload)
            if response.status_code != 200:
                _raise_openrouter_error(response.status_code, response.text)
            response_data = response.json()
            msg = response_data["choices"][0]["message"]
            logger.debug(format_completion("non-streaming", msg.get("reasoning", ""), msg.get("content", ""), response_data.get("usage"), model))
            return {
                "reasoning": msg.get("reasoning", ""),
                "response": msg.get("content", ""),
                "usage": response_data.get("usage"),
                "model": model,
                "tool_calls": _normalize_tool_calls(msg.get("tool_calls")),
            }
        except requests.RequestException as e:
            logger.error(f"Error invoking OpenRouter API (non-streaming): {e}")
            raise
        except (KeyError, IndexError) as e:
            logger.error(f"Unexpected response format from OpenRouter API: {e}")
            raise
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            raise

    def invoke_embeddings(self, input: list[str], model: str) -> list[list[float]]:
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