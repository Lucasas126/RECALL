from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel


def format_completion(kind: str, reasoning: str, response: str, usage, model: str) -> str:
    """Render a model completion as a readable multi-line block for the logs."""
    return (
        f"\n=== {kind} response ===\n"
        f"model: {model}\n"
        f"usage: {usage}\n"
        f"\n-- reasoning --\n{reasoning or '(none)'}\n"
        f"\n-- response --\n{response or '(none)'}\n"
    )


class LLMProvider(BaseModel):

    # name that config providers must match to select this class
    name: ClassVar[str] = ""

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None, tools=None):
        """
        Invokes the LLM provider API with the given messages and model.
        If stream is True, it will print the response in real-time.
        If reasoning is True, the provider requests and separates thinking tokens;
        reasoning_tokens optionally caps the reasoning budget.
        If tools is given (OpenAI-style specs), the model may return tool_calls.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")

    def invoke_embeddings(self, input: list[str], model: str):
        """
        Invokes the LLM provider API to get embeddings for a batch of texts.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")

    def context_window(self, model: str):
        """Return the model's maximum context length in tokens, or None if unknown."""
        return None


def get_provider(name: str, **kwargs) -> LLMProvider:
    """Return an instance of the LLMProvider subclass whose `name` matches."""
    for cls in LLMProvider.__subclasses__():
        if cls.name == name:
            return cls(**kwargs)
    raise ValueError(f"unknown provider: {name!r}")