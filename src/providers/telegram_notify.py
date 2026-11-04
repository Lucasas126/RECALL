from __future__ import annotations

import os
import re

import httpx
import telegramify_markdown

from src.utils.logger import get_logger

logger = get_logger("telegram")

BOT_TOKEN_ENV = "TELEGRAM_BOT_TOKEN"


def parse_allowed_users(raw: str) -> tuple[dict[int, str], list[str]]:
    """Parse `telegram_id=recall_user_id` entries; returns the mapping and the invalid entries."""
    allowed: dict[int, str] = {}
    invalid: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            telegram_id, user_id = (part.strip() for part in entry.split("=", 1))
            parsed_id = int(telegram_id)
        except (ValueError, TypeError):
            invalid.append(entry)
            continue
        if not user_id:
            invalid.append(entry)
            continue
        allowed[parsed_id] = user_id
    return allowed, invalid


class TelegramNotifier:
    """Sends plain messages to the private Telegram chats linked to RECALL users, via the Bot API."""

    def __init__(self, token: str, allowed: dict[int, str], *, timeout: float = 10.0):
        self._token = token
        self._chats: dict[str, list[int]] = {}
        for telegram_id, user_id in allowed.items():
            self._chats.setdefault(user_id, []).append(telegram_id)
        self._timeout = timeout

    @classmethod
    def from_env(cls, allowed_users_env: str) -> "TelegramNotifier | None":
        """A notifier when a bot token is configured, else None (notifications are then skipped)."""
        token = os.getenv(BOT_TOKEN_ENV, "").strip()
        if not token:
            return None
        allowed, _ = parse_allowed_users(os.getenv(allowed_users_env, ""))
        return cls(token, allowed)

    def linked(self, user_id: str) -> bool:
        return bool(self._chats.get(user_id))

    def send(self, user_id: str, text: str) -> int:
        """Send `text` to every Telegram chat linked to the user; returns how many were sent.

        URLs in the text are sent as explicit `text_link` entities: Telegram's own auto-linking skips
        hosts without a public domain (`localhost`, LAN names) and can cut the `#...` fragment, so the
        dream session link would not be clickable. If Telegram rejects the entities, the plain text is
        sent instead.
        """
        entities = link_entities(text)
        sent = 0
        for chat_id in self._chats.get(user_id, []):
            payload = {"chat_id": chat_id, "text": text}
            if entities:
                payload["entities"] = entities
            response = self._post(payload)
            if response.status_code == 400 and entities:
                response = self._post({"chat_id": chat_id, "text": text})
            if response.status_code != 200:
                raise RuntimeError(f"Telegram sendMessage failed with HTTP {response.status_code}")
            sent += 1
        return sent

    def send_markdown(self, user_id: str, text: str) -> int:
        """Like `send`, with `text` as Markdown converted to Telegram entities (plain text if that fails)."""
        try:
            plain, converted = telegramify_markdown.convert(text)
            entities = [entity.to_dict() for entity in converted]
        except Exception:
            logger.warning("Could not format the Telegram notification; sending plain text")
            return self.send(user_id, text)
        sent = 0
        for chat_id in self._chats.get(user_id, []):
            response = self._post({"chat_id": chat_id, "text": plain, "entities": entities})
            if response.status_code == 400:
                response = self._post({"chat_id": chat_id, "text": plain})
            if response.status_code != 200:
                raise RuntimeError(f"Telegram sendMessage failed with HTTP {response.status_code}")
            sent += 1
        return sent

    def _post(self, payload: dict) -> httpx.Response:
        try:
            return httpx.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage", json=payload, timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            # httpx errors carry the request URL, which contains the bot token: never log them as is
            raise RuntimeError(f"Telegram sendMessage failed: {type(exc).__name__}") from None


_URL_RE = re.compile(r"https?://\S+")


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def link_entities(text: str) -> list[dict]:
    """`text_link` entities for every http(s) URL in `text` (Telegram offsets are UTF-16 code units)."""
    return [
        {"type": "text_link", "offset": _utf16_len(text[:m.start()]), "length": _utf16_len(m.group(0)), "url": m.group(0)}
        for m in _URL_RE.finditer(text)
    ]
