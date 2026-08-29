from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel


class LLMProvider(BaseModel):

    # name that config providers must match to select this class
    name: ClassVar[str] = ""

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None):
        """
        Invokes the LLM provider API with the given messages and model.
        If stream is True, it will print the response in real-time.
        If reasoning is True, the provider requests and separates thinking tokens;
        reasoning_tokens optionally caps the reasoning budget.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")

    def invoke_embeddings(self, text: str, model: str):
        """
        Invokes the LLM provider API to get embeddings for the given text.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")


def get_provider(name: str, **kwargs) -> LLMProvider:
    """Return an instance of the LLMProvider subclass whose `name` matches."""
    for cls in LLMProvider.__subclasses__():
        if cls.name == name:
            return cls(**kwargs)
    raise ValueError(f"unknown provider: {name!r}")