from __future__ import annotations

import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Annotated, Callable

from src.agent.tooling import tool
from src.config.settings import CONFIG_DIR, TELEGRAM_ALLOWED_USERS_ENV
from src.config.spaces import notification_targets
from src.providers.telegram_notify import TelegramNotifier
from src.utils.logger import get_logger

if TYPE_CHECKING:
    from src.agent.agent import Agent

logger = get_logger("notify")

MAX_PER_HOUR = 30
MAX_CHARS = 3500  # one Telegram message holds 4096 characters; the rest is headroom for the formatting
_WINDOW_SECONDS = 3600.0

_sent: dict[tuple[str, str], deque[float]] = {}
_lock = threading.Lock()


def _take_slot(user_id: str, agent_id: str, now: float | None = None) -> int | None:
    """Count one notice against the agent's hourly limit; None when allowed, else the seconds until a slot frees up.

    The count lives in this process: a restart starts it again, which is fine for a guard against a looping agent.
    """
    now = time.monotonic() if now is None else now
    with _lock:
        window = _sent.setdefault((user_id, agent_id), deque())
        while window and now - window[0] >= _WINDOW_SECONDS:
            window.popleft()
        if len(window) >= MAX_PER_HOUR:
            return int(_WINDOW_SECONDS - (now - window[0])) + 1
        window.append(now)
    return None


def _forget_slot(user_id: str, agent_id: str) -> None:
    with _lock:
        window = _sent.get((user_id, agent_id))
        if window:
            window.pop()


def build_notify_tools(
    agent: Agent, notifier_factory: Callable[[], TelegramNotifier | None] | None = None,
) -> list[Callable[..., str]]:
    """The `notify` group: one tool that writes to the Telegram chat of the user who owns the agent.

    The recipient is never a parameter: the message always goes to the owner's linked chats, so the agent cannot
    write to anyone else.
    """
    factory = notifier_factory or (lambda: TelegramNotifier.from_env(TELEGRAM_ALLOWED_USERS_ENV))
    user_id, agent_id = agent.config.user_id, agent.config.id

    @tool
    def notify_telegram(
        text: Annotated[str, "the message, short and in Markdown; it goes to the user's own Telegram chat"],
    ) -> str:
        """Send the user a short message on Telegram, for example when a long task is done or something needs them.
        It goes only to the user's own chat. Send it last: do not announce a file or a result you have not delivered yet.
        At most 30 a hour; do not use it for chatter."""
        text = (text or "").strip()
        if not text:
            return "error: the message is empty"
        if len(text) > MAX_CHARS:
            return f"error: the message has {len(text)} characters; keep it under {MAX_CHARS}"
        notifier = factory()
        if notifier is None:
            return "error: Telegram is not set up on this server (no bot token), so nothing was sent"
        people = [person for person in notification_targets(user_id, CONFIG_DIR) if notifier.linked(person)]
        if not people:
            return "error: this user has no Telegram account linked, so nothing was sent"
        delivered, errors = 0, []
        for person in people:
            wait = _take_slot(person, agent_id)
            if wait is not None:
                errors.append(f"limit of {MAX_PER_HOUR} notices an hour reached; try again in {wait // 60 + 1} minutes")
                continue
            try:
                notifier.send_markdown(person, text)
                delivered += 1
            except Exception as exc:  # one member's failure never stops the others
                _forget_slot(person, agent_id)
                logger.warning("notify_telegram failed for %s/%s (member %s): %s", user_id, agent_id, person, exc)
                errors.append(f"Telegram did not accept the message ({exc})")
        if not delivered:
            return f"error: {errors[0]}"
        return "sent to the user's Telegram chat" if not errors else f"sent to {delivered} of {len(people)} members' Telegram chats"

    return [notify_telegram]
