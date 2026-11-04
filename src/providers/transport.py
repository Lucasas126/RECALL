from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from typing import Awaitable, Callable, Generator, Iterator, TypeVar

import httpx
import requests

from src.providers.cancellation import CancellationToken

_T = TypeVar("_T")

_local = threading.local()


def http_session() -> requests.Session:
    """A keep-alive `requests` session for this thread (sessions are not shared across threads).

    Short calls such as embeddings reuse one TLS connection instead of opening a new one each time,
    which saves the DNS+TCP+TLS handshake (roughly 60-100 ms per call to OpenRouter).
    """
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    return session


def post_json(url: str, **kwargs) -> requests.Response:
    """POST over this thread's keep-alive session; one retry on a connection the server had already closed."""
    try:
        return http_session().post(url, **kwargs)
    except requests.ConnectionError:
        http_session().close()
        return http_session().post(url, **kwargs)


def _run(
    runner: asyncio.Runner,
    operation: Callable[[], Awaitable[_T]],
    cancellation: CancellationToken | None,
) -> _T:
    async def execute() -> _T:
        if cancellation is None:
            return await operation()
        task = asyncio.current_task()
        assert task is not None
        with cancellation._bind(task):
            result = await operation()
            cancellation.raise_if_cancelled()
            return result

    try:
        return runner.run(execute())
    except asyncio.CancelledError:
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        raise


@contextmanager
def _response(
    url: str,
    payload: dict,
    headers: dict[str, str] | None,
    cancellation: CancellationToken | None,
) -> Generator[tuple[asyncio.Runner, httpx.Response]]:
    # Each synchronous invocation owns its loop; cancellation can come from another thread.
    with asyncio.Runner() as runner:
        client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=30))
        response = None
        try:
            request = client.build_request("POST", url, json=payload, headers=headers)
            response = _run(runner, lambda: client.send(request, stream=True), cancellation)
            if response.is_error:
                _run(runner, response.aread, cancellation)
                response.raise_for_status()
            yield runner, response
        finally:
            try:
                if response is not None:
                    runner.run(response.aclose())
            finally:
                runner.run(client.aclose())


def post_chat(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    cancellation: CancellationToken | None = None,
) -> dict:
    """Read a chat response, cancelling even while waiting for headers or the body."""
    with _response(url, payload, headers, cancellation) as (runner, response):
        _run(runner, response.aread, cancellation)
        return response.json()


def stream_chat(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
    cancellation: CancellationToken | None = None,
) -> Iterator[str]:
    """Yield decoded lines and close the HTTP connection on completion or cancellation."""
    with _response(url, payload, headers, cancellation) as (runner, response):
        lines = response.aiter_lines()
        while True:
            try:
                yield _run(runner, lines.__anext__, cancellation)
            except StopAsyncIteration:
                return
