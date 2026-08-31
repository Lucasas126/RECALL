import os
import requests
import json
from typing import ClassVar

from src.providers.provider import LLMProvider, format_completion
from src.utils.logger import get_logger

import dotenv

dotenv.load_dotenv()
logger = get_logger("provider-openrouter")


class OpenRouterProvider(LLMProvider):

    name: ClassVar[str] = "openrouter"

    api_key: str = ""

    def __init__(self, **data):
        super().__init__(**data)
        # self.api_key = os.environ.get("OPENROUTER_API_KEY", "")

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None):
        """
        Invokes the OpenRouter API with the given messages and model.
        If stream is True, yields (kind, text) tuples then a final dict with usage info.
        If stream is False, returns a dict directly.
        """
        logger.debug("request model=%s stream=%s reasoning=%s msgs=%d", model, stream, reasoning, len(messages))
        if stream:
            return self.invoke_stream(messages, model, self.api_key, reasoning=reasoning, reasoning_tokens=reasoning_tokens)
        return self.invoke_sync(messages, model, self.api_key, reasoning=reasoning, reasoning_tokens=reasoning_tokens)

    def invoke_stream(self, messages: list[dict], model: str, api_key: str, reasoning=False, reasoning_tokens=None):
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

        try:
            response_text = ""
            reasoning_text = ""
            usage_info = None
            buffer = ""
            with requests.post(url, headers=headers, json=payload, stream=True) as r:
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
                                    delta = data_obj["choices"][0]["delta"]

                                    reasoning_chunk = delta.get("reasoning")
                                    if reasoning_chunk:
                                        reasoning_text += reasoning_chunk
                                        yield ("reasoning", reasoning_chunk)

                                    content = delta.get("content")
                                    if content:
                                        response_text += content
                                        yield ("content", content)

                                    # Capture usage info if present in the final message
                                    if "usage" in data_obj:
                                        usage_info = data_obj["usage"]
                                except json.JSONDecodeError:
                                    pass

                        except Exception:
                            break

            # Yield the final result with usage info after streaming is complete
            logger.debug(format_completion("streaming", reasoning_text, response_text, usage_info, model))
            yield {"reasoning": reasoning_text, "response": response_text, "usage": usage_info, "model": model}

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
                    "model": model
                }
            except Exception as retry_error:
                logger.error(f"Fallback to non-streaming also failed: {retry_error}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error during streaming: {e}")
            raise

    def invoke_sync(self, messages: list[dict], model: str, api_key: str, reasoning=False, reasoning_tokens=None) -> dict:
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
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            response_data = response.json()
            msg = response_data["choices"][0]["message"]
            logger.debug(format_completion("non-streaming", msg.get("reasoning", ""), msg.get("content", ""), response_data.get("usage"), model))
            return {
                "reasoning": msg.get("reasoning", ""),
                "response": msg.get("content", ""),
                "usage": response_data.get("usage"),
                "model": model
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