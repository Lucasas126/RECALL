"""Provider availability and per-provider model catalogs for the model picker.

There is no per-user "enabled" setting: a provider is selectable when the server can use it (OpenRouter: its
API key variable is set; Ollama: the server answers; Claude subscription: Claude Code is installed and logged in).
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from time import monotonic
from typing import Callable, Literal

from src.config.model_refs import (
    CAPABILITIES, CLAUDE_SUBSCRIPTION, LABELS, OLLAMA, OPENROUTER, PROVIDERS, model_name, prefix_of, reference,
    split_reference,
)
from src.config.models import UserConfig
from src.providers.catalog import CatalogUnavailable, list_openrouter_models, list_openrouter_transcription_models
from src.providers.claude_subscription.catalog import catalog_entries as claude_catalog_entries
from src.providers.claude_subscription.status import cli_status
from src.providers.ollama import OllamaUnavailable, is_ollama_cloud, list_ollama_models, ollama_host
from src.utils.i18n import InputError

CatalogCapability = Literal["chat", "stt"]
CatalogError = CatalogUnavailable | OllamaUnavailable
AVAILABILITY_TTL_SECONDS = 60.0


def providers_for(capability: str) -> list[str]:
    """The providers able to serve `capability`, in picker order."""
    return [name for name in PROVIDERS if capability in CAPABILITIES[name]]


def _row(provider: str, main: str, *, available: bool, reason: str | None = None, detail: str | None = None) -> dict:
    """One provider's availability. `reason` is a code the browser translates; `detail` is opaque text (a variable
    name, a host, the Claude Code hint) and never a secret value."""
    return {
        "id": provider, "label": LABELS[provider], "main": provider == main, "available": available,
        "reason": reason, "detail": detail, "experimental": provider == CLAUDE_SUBSCRIPTION,
        "zdr_applies": provider == OPENROUTER, "capabilities": sorted(CAPABILITIES[provider]),
    }


def _openrouter(user: UserConfig) -> dict:
    variable = user.openrouter_settings().api_key
    found = bool(os.environ.get(variable, "").strip())
    return _row(OPENROUTER, user.provider.name, available=found, reason=None if found else "no_api_key", detail=variable)


def _ollama(user: UserConfig) -> dict:
    host = ollama_host(user.ollama.host)
    try:
        list_ollama_models(user.ollama.host, timeout=3)
    except OllamaUnavailable:
        return _row(OLLAMA, user.provider.name, available=False, reason="unreachable", detail=host)
    return _row(OLLAMA, user.provider.name, available=True, detail=host)


def _claude(user: UserConfig) -> dict:
    status = cli_status(user.claude_subscription)
    main = user.provider.name
    if not status["cli_found"]:
        return _row(CLAUDE_SUBSCRIPTION, main, available=False, reason="cli_missing", detail=status["hint"])
    if status["logged_in"] is None:
        return _row(CLAUDE_SUBSCRIPTION, main, available=False, reason="cli_unknown", detail=status["hint"])
    if not status["logged_in"]:
        return _row(CLAUDE_SUBSCRIPTION, main, available=False, reason="logged_out", detail=status["hint"])
    return _row(CLAUDE_SUBSCRIPTION, main, available=True)


class ProviderAvailability:
    """Which providers a user can pick right now. Slow checks (Ollama, Claude Code) are cached briefly, so
    opening the picker does not start a CLI process each time; the OpenRouter key check is a cheap env lookup."""

    def __init__(self, ttl: float = AVAILABILITY_TTL_SECONDS, clock: Callable[[], float] = monotonic) -> None:
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[tuple, tuple[float, dict]] = {}

    def _cached(self, key: tuple, check: Callable[[], dict]) -> dict:
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and self._clock() - hit[0] < self._ttl:
                return hit[1]
        row = check()
        with self._lock:
            self._cache[key] = (self._clock(), row)
        return row

    def forget(self) -> None:
        """Drop cached checks (the next call asks the providers again)."""
        with self._lock:
            self._cache.clear()

    def rows(self, user: UserConfig, capability: str = "chat") -> list[dict]:
        """Availability of every provider able to serve `capability` (checked concurrently)."""
        main = user.provider.name
        claude = user.claude_subscription
        checks: dict[str, Callable[[], dict]] = {
            OPENROUTER: lambda: _openrouter(user),
            OLLAMA: lambda: self._cached((OLLAMA, main, user.ollama.host), lambda: _ollama(user)),
            CLAUDE_SUBSCRIPTION: lambda: self._cached(
                (CLAUDE_SUBSCRIPTION, main, claude.claude_config_dir, claude.command), lambda: _claude(user)),
        }
        names = providers_for(capability)
        with ThreadPoolExecutor(max_workers=len(names) or 1) as pool:
            return list(pool.map(lambda name: checks[name](), names))

    def require(self, user: UserConfig, provider: str, capability: str) -> None:
        """Raise an actionable error unless `provider` can serve `capability` right now."""
        if capability not in CAPABILITIES[provider]:
            raise InputError("{provider} cannot serve this kind of model.", provider=LABELS[provider])
        row = next(row for row in self.rows(user, capability) if row["id"] == provider)
        if not row["available"]:
            raise unavailable(row)


def unavailable(row: dict) -> InputError:
    """The error for choosing a provider that cannot be used now, naming what to fix."""
    label = LABELS[row["id"]]
    reason = row["reason"]
    if reason == "no_api_key":
        return InputError("Provider not available: {provider}. The API key variable {variable} is not set on the server.",
                          provider=label, variable=row["detail"])
    if reason == "unreachable":
        return InputError("Provider not available: {provider}. The server at {host} did not answer.",
                          provider=label, host=row["detail"])
    if reason == "cli_missing":
        return InputError("Provider not available: {provider}. Claude Code is not installed on the computer running RECALL.",
                          provider=label)
    if reason == "logged_out":
        return InputError("Provider not available: {provider}. Claude Code is not logged in; run `claude auth login` on the "
                          "computer running RECALL.", provider=label)
    return InputError("Provider not available: {provider}. Claude Code did not report its login state; run `claude auth status`.",
                      provider=label)


def _decorate(row: dict, provider: str, main: str) -> dict:
    row.update({
        "provider": provider, "provider_label": LABELS[provider], "main_provider": provider == main,
        "zdr_applies": provider == OPENROUTER,
    })
    return row


def provider_rows(user: UserConfig, provider: str, query: str = "", *, zdr: bool, capability: CatalogCapability) -> list[dict]:
    """Picker rows of one provider; ids are model references (bare for the main provider)."""
    main = user.provider.name
    if capability not in CAPABILITIES[provider]:
        return []
    named = prefix_of(query)
    if named is not None and named != provider:
        return []
    needle = model_name(query)
    if provider == OPENROUTER:
        found = (list_openrouter_models(needle, zdr=zdr) if capability == "chat"
                 else list_openrouter_transcription_models(needle, zdr=zdr))
        return [_decorate(m.model_dump() | {"id": reference(OPENROUTER, m.id, main)}, OPENROUTER, main) for m in found]
    if provider == OLLAMA:
        text = needle.strip().casefold()
        return [_decorate({
            "id": reference(OLLAMA, name, main), "name": name,
            "description": "Ollama cloud model (runs on Ollama's servers)" if is_ollama_cloud(name) else "Local Ollama model",
            "zdr_providers": [], "pricing": {"prompt": None, "completion": None}, "input_modalities": ["text"],
            "parameter_routes": [], "local": not is_ollama_cloud(name),
        }, OLLAMA, main) for name in list_ollama_models(user.ollama.host) if not text or text in name.casefold()]
    return [_decorate(row, CLAUDE_SUBSCRIPTION, main) for row in claude_catalog_entries(needle)]


def canonical_reference(user: UserConfig, model: str) -> tuple[str, str]:
    """`(provider, canonical reference)` of `model`: bare for the main provider, prefixed otherwise."""
    provider, name = split_reference(model, user.provider.name)
    return provider, reference(provider, name, user.provider.name)


def require_listed(provider: str, canonical: str, capability: str, rows: list[dict]) -> None:
    """Raise unless the catalog rows offer `canonical` (a reference as `reference()` builds it)."""
    if any(row["id"] == canonical for row in rows):
        return
    if provider == OPENROUTER and capability == "stt":
        raise InputError("The speech-to-text model is not available with the required routing policy.")
    if provider == OPENROUTER:
        raise InputError("model is not available in the OpenRouter chat catalog with the selected ZDR filter")
    if provider == OLLAMA:
        raise InputError("The model {model} is not installed on the Ollama server.", model=model_name(canonical))
    raise InputError("{model} is not one of the offered Claude subscription models.", model=model_name(canonical))
