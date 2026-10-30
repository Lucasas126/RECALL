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

    def cancel(self) -> bool:
        with self._lock:
            if self._finished:
                return False
            self._cancelled = True
            for task in self._tasks:
                task.get_loop().call_soon_threadsafe(task.cancel)
            return True

    @contextmanager
    def _commit(self) -> Generator[None]:
        with self._lock:
            if self._cancelled:
                raise GenerationCancelled("generation interrupted by user")
            yield
            self._finished = True

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
