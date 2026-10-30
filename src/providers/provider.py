from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from src.providers.cancellation import CancellationToken


class UnsupportedModalityError(RuntimeError):
    """The selected model cannot accept one of the requested input modalities."""


def unsupported_modality_message(language: str, modality: str) -> str:
    """Return a localized explanation for an unsupported attachment modality."""
    messages = {
        "en": "This model does not support {modality} input.",
        "es": "Este modelo no admite entradas de {modality}.",
    }
    labels = {
        "en": {"image": "image", "video": "video", "audio": "audio"},
        "es": {"image": "imagen", "video": "vídeo", "audio": "audio"},
    }
    language = language.split("-", 1)[0].lower()
    language_messages = messages.get(language, messages["en"])
    language_labels = labels.get(language, labels["en"])
    return language_messages.format(modality=language_labels.get(modality, modality))


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

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None, *, zdr: bool = True):
        """
        Invokes the LLM provider API with the given messages and model.
        If stream is True, it will print the response in real-time.
        If reasoning is True, the provider requests and separates thinking tokens;
        reasoning_tokens optionally caps the reasoning budget.
        If tools is given (OpenAI-style specs), the model may return tool_calls.
        Cancellation raises GenerationCancelled rather than returning a partial success.
        zdr requires zero-retention routing where supported; local providers ignore it.
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

    def supported_modalities(self, model: str) -> set[str]:
        """Return the input modalities explicitly supported by a model."""
        return set()


def get_provider(name: str, **kwargs) -> LLMProvider:
    """Return an instance of the LLMProvider subclass whose `name` matches."""
    for cls in LLMProvider.__subclasses__():
        if cls.name == name:
            return cls(**kwargs)
    raise ValueError(f"unknown provider: {name!r}")