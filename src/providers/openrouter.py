import os
import requests
import json
import logging
from src.providers.provider import LLMProvider

import dotenv

dotenv.load_dotenv()
logger = logging.getLogger(__name__)


class OpenRouterProvider(LLMProvider):

    api_key: str = ""

    def __init__(self, **data):
        super().__init__(**data)
        self.api_key = os.environ.get("OPENROUTER_API_KEY", "")

    def invoke(self, messages: list[dict], model: str, stream=True):
        """
        Invokes the OpenRouter API with the given messages and model.
        If stream is True, yields text chunks and a final dict with usage info.
        If stream is False, returns a dict directly.
        """
        if stream:
            return self.invoke_stream(messages, model, self.api_key)
        return self.invoke_sync(messages, model, self.api_key)

    def invoke_stream(self, messages: list[dict], model: str, api_key: str):
        """Streaming variant — yields str chunks then a final dict with usage info."""

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

        try:
            response_text = ""
            usage_info = None
            buffer = ""
            with requests.post(url, headers=headers, json=payload, stream=True) as r:
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
                                    content = data_obj["choices"][0]["delta"].get("content")
                                    if content:
                                        response_text += content 
                                        yield content # Yield each chunk of content as it arrives

                                    # Capture usage info if present in the final message
                                    if "usage" in data_obj:
                                        usage_info = data_obj["usage"]
                                except json.JSONDecodeError:
                                    pass

                        except Exception:
                            break

            # Yield the final result with usage info after streaming is complete
            yield {"response": response_text, "usage": usage_info, "model": model}

        except requests.RequestException as e:
            logger.warning(f"Error during streaming from OpenRouter API: {e}, retrying without streaming")
            try:
                response = requests.post(url, headers=headers, json={**payload, "stream": False})
                response.raise_for_status()
                response_data = response.json()
                yield {
                    "response": response_data["choices"][0]["message"]["content"],
                    "usage": response_data.get("usage"),
                    "model": model
                }
            except Exception as retry_error:
                logger.error(f"Fallback to non-streaming also failed: {retry_error}")
                raise
        except Exception as e:
            logger.error(f"Unexpected error during streaming: {e}")
            raise

    def invoke_sync(self, messages: list[dict], model: str, api_key: str) -> dict:
        """Non-streaming variant — returns a dict with response, usage and model."""
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        payload = {"model": model, "messages": messages, "stream": False}
        try:
            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            response_data = response.json()
            return {
                "response": response_data["choices"][0]["message"]["content"],
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
        response = requests.post(
            "https://openrouter.ai/api/v1/embeddings",
            json=payload,
            headers=headers,
            timeout=60,
        )
        response.raise_for_status()
        data = response.json()["data"]
        # data is a list of {"index": int, "embedding": list[float]}
        data.sort(key=lambda x: x["index"])
        return [item["embedding"] for item in data]