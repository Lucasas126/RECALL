import requests
import json
import logging
from src.providers.provider import LLMProvider

logger = logging.getLogger(__name__)


class OpenRouterProvider(LLMProvider):

    def invoke(self, messages: list[dict], model: str, api_key: str, stream=True):
        """
        Invokes the OpenRouter API with the given messages and model.
        If stream is True, it will print the response in real-time.
        Returns a dictionary containing the response, the usage information, and the model used.
        """

        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": model,
            "messages": messages,
            "stream": stream
        }

        if stream:
            try:
                response_text = ""
                usage_info = None
                buffer = ""
                with requests.post(url, headers=headers, json=payload, stream=True) as r:
                    for chunk in r.iter_content(chunk_size=1024, decode_unicode=True):
                        buffer += chunk
                        while True:
                            try:
                                # Find the next complete SSE line
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
                                            yield content  # Yield each chunk of content as it arrives
                                        
                                        # Capture usage info if present (comes at the end)
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
                
        else:
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
