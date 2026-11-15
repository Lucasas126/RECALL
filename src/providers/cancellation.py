from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Generator


class GenerationCancelled(RuntimeError):
    """The caller interrupted a generation; this is not a provider failure."""


class CancellationToken:
    """A one-shot, thread-safe cancellation signal for one generation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._finished = False
        self._tasks: set[asyncio.Task] = set()
        self._children: set[CancellationToken] = set()
        self.reason: str | None = None  # "stopped" (the stop button) or "replaced" (a new message); None: other

    def cancel(self, reason: str | None = None) -> bool:
        with self._lock:
            if self._finished:
                return False
            if self.reason is None:  # the first cause wins
                self.reason = reason
            self._cancelled = True
            for task in self._tasks:
                task.get_loop().call_soon_threadsafe(task.cancel)
            for child in self._children:
                child.cancel()
            return True

    @contextmanager
    def _link(self, child: "CancellationToken") -> Generator[None]:
        """Cancel `child` whenever this token is cancelled; cancelling `child` leaves this token alone."""
        with self._lock:
            if self._cancelled:
                raise GenerationCancelled("generation interrupted by user")
            self._children.add(child)
        try:
            yield
        finally:
            with self._lock:
                self._children.discard(child)

    @contextmanager
    def _commit(self) -> Generator[None]:
        with self._lock:
            if self._cancelled:
                raise GenerationCancelled("generation interrupted by user")
            yield
            self._finished = True

    def finish(self) -> None:
        """Mark the generation committed without running it: cancelling it afterwards is a no-op."""
        with self._lock:
            self._finished = True

    def is_cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def raise_if_cancelled(self) -> None:
        with self._lock:
            if self._cancelled:
                raise GenerationCancelled("generation interrupted by user")

    @contextmanager
    def _bind(self, task: asyncio.Task) -> Generator[None]:
        with self._lock:
            if self._cancelled:
                raise GenerationCancelled("generation interrupted by user")
            self._tasks.add(task)
        try:
            yield
        finally:
            with self._lock:
                self._tasks.remove(task)
