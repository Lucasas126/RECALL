"""Post-processing of command output before the model sees it: strip ANSI, redact secrets, truncate."""

from __future__ import annotations

import re

from src.config.secrets import file_values

_ANSI = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"      # OSC ... BEL / ST
    r"|\x1b\[[0-?]*[ -/]*[@-~]"               # CSI
    r"|\x1b[@-Z\\-_]"                         # two-character escapes
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

REDACTED = "[REDACTED]"
_SECRETS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S),
     "[REDACTED PRIVATE KEY]"),
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[^\s\"']+"), r"\1" + REDACTED),
    (re.compile(r"\bsk-or-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9]{32,}"), REDACTED),
    (re.compile(r"\btvly-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), REDACTED),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), REDACTED),
    (re.compile(r"(?<![A-Za-z0-9])\d{6,}:[A-Za-z0-9_-]{30,}"), REDACTED),  # Telegram bot tokens
)


def strip_ansi(text: str) -> str:
    return _CONTROL.sub("", _ANSI.sub("", text.replace("\r\n", "\n")))


MIN_SECRET_LENGTH = 8  # shorter values are too likely to match ordinary text


def redact(text: str) -> str:
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    # exact values stored in .config/secrets.env, whatever their shape (longest first)
    for value in sorted({v for v in file_values() if len(v) >= MIN_SECRET_LENGTH}, key=len, reverse=True):
        text = text.replace(value, REDACTED)
    return text


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head and the tail of `text` within `limit` characters; returns (text, was_truncated)."""
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 5
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n[... {omitted} characters omitted ...]\n{text[-tail:]}", True


def clean(text: str, limit: int) -> tuple[str, bool]:
    """ANSI-free, redacted and truncated output (redaction first, so a cut cannot hide half a secret)."""
    return truncate(redact(strip_ansi(text)), limit)
