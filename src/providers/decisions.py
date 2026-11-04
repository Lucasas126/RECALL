"""Client of the OpenRouter Decisions API (alpha): a fast model that answers one multiple-choice question.

The endpoint is an alpha API and may change; everything that touches it lives here. The question id inside
`questions` is never seen by the model, so the whole meaning is carried by `instructions` and the criteria texts.
Only the given state (the user's message) and the criteria are sent: no history, no attachments.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import requests

from src.providers.transport import http_session
from src.utils.logger import get_logger

logger = get_logger("provider-decisions")

DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
_QUESTION_ID = "agent"


class DecisionError(RuntimeError):
    """The decision model could not give a usable answer (network, HTTP error, malformed or unknown answer)."""


@dataclass(frozen=True)
class Decision:
    choice: str
    confidence: float
    probabilities: dict[str, float] = field(default_factory=dict)
    latency_ms: float = 0.0
    cost: float | None = None


def build_request(
    *, model: str, state: str, instructions: str, criteria: dict[str, str], zdr: bool,
) -> dict:
    """The exact JSON body of one single-choice decision; `provider.zdr` is sent only when ZDR is required."""
    body: dict = {
        "model": model,
        "state": state,
        "questions": {_QUESTION_ID: {"type": "choice", "instructions": instructions, "criteria": dict(criteria)}},
    }
    if zdr:
        body["provider"] = {"zdr": True}
    return body


def decide_choice(
    *, api_key: str, model: str, state: str, instructions: str, criteria: dict[str, str],
    zdr: bool = False, timeout: float = 4,
) -> Decision:
    """Ask the model to choose one criteria id; raises DecisionError on any failure (never returns a guess)."""
    if not api_key:
        raise DecisionError("no API key for the decision model")
    body = build_request(model=model, state=state, instructions=instructions, criteria=criteria, zdr=zdr)
    started = time.monotonic()
    try:
        response = http_session().post(
            DECISIONS_URL, json=body, timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        raise DecisionError(f"decision request failed: {type(exc).__name__}") from exc
    latency_ms = (time.monotonic() - started) * 1000
    if response.status_code >= 400:
        raise DecisionError(f"decision model returned HTTP {response.status_code}")
    try:
        payload = response.json()
        answer = payload["answers"][_QUESTION_ID]
        choice = answer["choice"]
        confidence = float(answer["confidence"])
        raw = answer.get("probabilities") or {}
        probabilities = {str(key): float(value) for key, value in raw.items()}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise DecisionError("decision model returned an unreadable answer") from exc
    if choice not in criteria:
        raise DecisionError("decision model chose an unknown option")
    if not 0 <= confidence <= 1:
        raise DecisionError("decision model returned an invalid confidence")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    cost = usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None
    probabilities = {key: value for key, value in probabilities.items() if key in criteria}
    logger.debug(
        "decision model=%s latency=%.0fms cost=%s choice=%s confidence=%.2f", model, latency_ms, cost, choice, confidence,
    )
    return Decision(choice, confidence, probabilities, latency_ms, cost)
