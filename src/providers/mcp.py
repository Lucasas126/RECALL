from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Coroutine, TypeVar

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.stdio import get_default_environment
from mcp_types import (
    CallToolResult,
    EmbeddedResource,
    PaginatedRequestParams,
    TextContent,
    TextResourceContents,
)

from src.config.models import McpEnvFromEnvironment, McpEnvUserFile, McpServerConfig, McpSettings
from src.utils.i18n import UserMessageError
from src.utils.logger import get_logger, run_log_dir

logger = get_logger("mcp")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_AUTH_URL_WAIT_SECONDS = 120
_CLOSE_WAIT_SECONDS = 10
_T = TypeVar("_T")


class McpError(UserMessageError, RuntimeError):
    """An MCP server could not be started, reached or used."""


class UnknownMcpServer(McpError, ValueError):
    """The requested server is not in the shared catalog."""


class McpNotConnected(McpError):
    """The user has not connected the account the server needs."""

    code = "mcp_not_connected"


@dataclass(frozen=True)
class McpTool:
    """A tool offered by an MCP server, as listed by the server."""

    name: str
    description: str
    input_schema: dict
    read_only: bool


def _error_text(error: BaseException) -> str:
    """Describe an error, unwrapping the task-group exception groups raised by the SDK."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return str(error) or type(error).__name__


class _EventLoop:
    """A private asyncio loop on a daemon thread, so synchronous callers can use the async SDK."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def run(self, coroutine: Coroutine[Any, Any, _T], timeout: float) -> _T:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._get())
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise

    def _get(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                threading.Thread(target=self._loop.run_forever, name="mcp-loop", daemon=True).start()
            return self._loop


class _Connection:
    """One running server process and its client session, owned by the event loop."""

    def __init__(self, params: StdioServerParameters, log_path: Path | None, call_timeout: float) -> None:
        self._params = params
        self._log_path = log_path
        self._call_timeout = call_timeout
        self._session: ClientSession | None = None
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self.tools: list[McpTool] = []

    @property
    def alive(self) -> bool:
        return self._session is not None and self._task is not None and not self._task.done()

    async def open(self, timeout: float) -> None:
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(ready))
        try:
            await asyncio.wait_for(asyncio.shield(ready), timeout)
        except BaseException:
            await self.close()
            raise

    async def _run(self, ready: asyncio.Future[None]) -> None:
        try:
            with self._open_log() as errlog:
                async with stdio_client(self._params, errlog) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=self._call_timeout) as session:
                        await session.initialize()
                        self.tools = await _list_tools(session)
                        self._session = session
                        ready.set_result(None)
                        await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(_error_text(exc)))
            elif not isinstance(exc, asyncio.CancelledError):
                logger.warning("MCP server %s stopped: %s", self._params.args, _error_text(exc))
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._session = None

    def _open_log(self) -> IO[str]:
        if self._log_path is None:
            return open(os.devnull, "w", encoding="utf-8")
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        return open(self._log_path, "a", encoding="utf-8")

    async def call(self, name: str, arguments: dict) -> CallToolResult:
        if self._session is None:
            raise RuntimeError("the MCP session is closed")
        return await self._session.call_tool(name, arguments, read_timeout_seconds=self._call_timeout)

    async def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is None or self._task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._task), _CLOSE_WAIT_SECONDS)
        except Exception:
            self._task.cancel()


async def _list_tools(session: ClientSession) -> list[McpTool]:
    tools: list[McpTool] = []
    cursor: str | None = None
    while True:
        page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
        for tool in page.tools:
            annotations = tool.annotations
            tools.append(McpTool(
                name=tool.name,
                description=tool.description or (annotations.title if annotations and annotations.title else ""),
                input_schema=dict(tool.input_schema or {"type": "object", "properties": {}}),
                read_only=bool(annotations and annotations.read_only_hint),
            ))
        cursor = page.next_cursor
        if not cursor:
            return tools


@dataclass
class _AuthFlow:
    """A running sign-in command for one user and server."""

    process: subprocess.Popen
    url_ready: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    url: str | None = None
    error: str | None = None


class McpManager:
    """Connects users to the shared MCP servers with per-user credentials and private folders.

    A server process is started lazily for each (user, server) and reused by all of that user's
    agents and chats. Its environment comes from the catalog: shared values are copied from
    RECALL's environment and `user_file` values point into `<config_dir>/<user_id>/mcp/<server_id>/`,
    which is also the process working directory, so users never share credentials.
    """

    def __init__(self, settings: McpSettings, config_dir: Path) -> None:
        self._settings = settings
        self._config_dir = config_dir
        self._loop = _EventLoop()
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._connections: dict[tuple[str, str], _Connection] = {}
        self._auth: dict[tuple[str, str], _AuthFlow] = {}

    # ------------------------------------------------------------------ catalog & status

    @property
    def servers(self) -> dict[str, McpServerConfig]:
        return dict(self._settings.servers)

    def server(self, server_id: str) -> McpServerConfig:
        try:
            return self._settings.servers[server_id]
        except KeyError:
            raise UnknownMcpServer("Unknown MCP server: {server_id}", server_id=server_id) from None

    def state_dir(self, user_id: str, server_id: str) -> Path:
        """Return the user's private folder for a server (credentials, tokens, working directory)."""
        if not _ID_RE.match(user_id):
            raise ValueError(f"invalid user id: {user_id!r}")
        self.server(server_id)
        return self._config_dir / user_id / "mcp" / server_id

    def is_connected(self, user_id: str, server_id: str) -> bool:
        """Whether the user has the credentials the server needs (always true without sign-in)."""
        auth = self.server(server_id).auth
        return auth is None or (self.state_dir(user_id, server_id) / auth.credential_file).is_file()

    def status(self, user_id: str, server_id: str) -> dict:
        server = self.server(server_id)
        flow = self._auth.get((user_id, server_id))
        pending = flow is not None and not flow.finished.is_set()
        return {
            "id": server_id,
            "name": server.name,
            "description": server.description,
            "requires_auth": server.auth is not None,
            "connected": self.is_connected(user_id, server_id),
            "pending": pending,
            "auth_url": flow.url if pending and flow is not None else None,
            "error": flow.error if flow is not None and flow.finished.is_set() else None,
        }

    # ------------------------------------------------------------------ tools

    def list_tools(self, user_id: str, server_id: str) -> list[McpTool]:
        """List the server's tools for this user, excluding the catalog's hidden tools."""
        excluded = set(self.server(server_id).exclude_tools)
        return [tool for tool in self._connection(user_id, server_id).tools if tool.name not in excluded]

    def call_tool(self, user_id: str, server_id: str, tool: str, arguments: dict) -> str:
        """Call a tool and return its text content; raises McpError when the server reports an error."""
        if tool in self.server(server_id).exclude_tools:
            raise McpError("The MCP tool '{tool}' is not available.", tool=tool)
        connection = self._connection(user_id, server_id)
        timeout = self._settings.call_timeout_seconds
        try:
            result = self._loop.run(connection.call(tool, arguments), timeout + 5)
        except concurrent.futures.TimeoutError:
            raise McpError(
                "The MCP server '{server}' did not respond within {seconds} seconds.",
                server=self.server(server_id).name, seconds=int(timeout),
            ) from None
        except Exception as exc:
            raise McpError("The MCP tool '{tool}' failed: {error}", tool=tool, error=_error_text(exc)) from exc
        text = _result_text(result)
        if result.is_error:
            raise McpError("The MCP tool '{tool}' failed: {error}", tool=tool, error=text or "unknown error")
        return text

    def _connection(self, user_id: str, server_id: str) -> _Connection:
        server = self.server(server_id)
        if not self.is_connected(user_id, server_id):
            raise McpNotConnected("Connect {server} in Settings before using it.", server=server.name)
        key = (user_id, server_id)
        with self._key_lock(key):
            connection = self._connections.get(key)
            if connection is not None and connection.alive:
                return connection
            params = StdioServerParameters(
                command=self._command(server_id),
                args=server.args,
                env=self._environment(user_id, server_id),
                cwd=self._prepared_state_dir(user_id, server_id),
            )
            connection = _Connection(params, self._log_path(user_id, server_id), self._settings.call_timeout_seconds)
            timeout = self._settings.connect_timeout_seconds
            try:
                self._loop.run(connection.open(timeout), timeout + _CLOSE_WAIT_SECONDS)
            except (TimeoutError, concurrent.futures.TimeoutError):
                raise McpError(
                    "The MCP server '{server}' did not respond within {seconds} seconds.",
                    server=server.name, seconds=int(timeout),
                ) from None
            except Exception as exc:
                raise McpError(
                    "Could not start the MCP server '{server}': {error}", server=server.name, error=_error_text(exc),
                ) from exc
            self._connections[key] = connection
            logger.info("started MCP server %s for user %s (%d tools)", server_id, user_id, len(connection.tools))
            return connection

    # ------------------------------------------------------------------ sign-in

    def start_auth(self, user_id: str, server_id: str) -> dict:
        """Start the server's sign-in for the user and return the status with the sign-in URL."""
        server = self.server(server_id)
        auth = server.auth
        if auth is None:
            raise McpError("{server} does not need a sign-in.", server=server.name)
        key = (user_id, server_id)
        with self._key_lock(key):
            flow = self._auth.get(key)
            if flow is None or flow.finished.is_set():
                self._close_connection(key)
                flow = self._spawn_auth(user_id, server_id)
                self._auth[key] = flow
        if not flow.url_ready.wait(_AUTH_URL_WAIT_SECONDS):
            flow.process.kill()
            raise McpError(
                "The sign-in for {server} did not start: {error}",
                server=server.name, error=f"no sign-in URL after {_AUTH_URL_WAIT_SECONDS} seconds",
            )
        if flow.url is None and flow.error is not None:
            raise McpError("The sign-in for {server} did not start: {error}", server=server.name, error=flow.error)
        return self.status(user_id, server_id)

    def disconnect(self, user_id: str, server_id: str) -> dict:
        """Stop the user's server and sign-in, and delete the user's stored credentials."""
        auth = self.server(server_id).auth
        key = (user_id, server_id)
        with self._key_lock(key):
            flow = self._auth.pop(key, None)
            if flow is not None and not flow.finished.is_set():
                flow.process.kill()
            self._close_connection(key)
            if auth is not None:
                (self.state_dir(user_id, server_id) / auth.credential_file).unlink(missing_ok=True)
        return self.status(user_id, server_id)

    def _spawn_auth(self, user_id: str, server_id: str) -> _AuthFlow:
        auth = self.server(server_id).auth
        assert auth is not None
        log_path = self._log_path(user_id, server_id)
        process = subprocess.Popen(
            [self._command(server_id), *auth.args],
            env=get_default_environment() | self._environment(user_id, server_id),
            cwd=self._prepared_state_dir(user_id, server_id),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        flow = _AuthFlow(process)
        pattern = re.compile(auth.url_pattern)
        credential = self.state_dir(user_id, server_id) / auth.credential_file
        timer = threading.Timer(auth.timeout_seconds, process.kill)
        timer.daemon = True

        def follow() -> None:
            assert process.stdout is not None
            with process.stdout, (open(log_path, "a", encoding="utf-8") if log_path else open(os.devnull, "w")) as log:
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    match = pattern.search(line)
                    if match and flow.url is None:
                        flow.url = match.group(1)
                        flow.url_ready.set()
            code = process.wait()
            timer.cancel()
            if code != 0 or not credential.is_file():
                flow.error = f"sign-in ended without credentials (exit code {code})"
                logger.warning("MCP sign-in for %s/%s failed with exit code %s", user_id, server_id, code)
            else:
                logger.info("MCP sign-in for %s/%s completed", user_id, server_id)
            flow.finished.set()
            flow.url_ready.set()

        threading.Thread(target=follow, name=f"mcp-auth-{server_id}", daemon=True).start()
        timer.start()
        return flow

    # ------------------------------------------------------------------ helpers

    def close(self) -> None:
        """Stop every server process and pending sign-in."""
        for flow in list(self._auth.values()):
            if not flow.finished.is_set():
                flow.process.kill()
        for key in list(self._connections):
            self._close_connection(key)

    def _close_connection(self, key: tuple[str, str]) -> None:
        connection = self._connections.pop(key, None)
        if connection is not None:
            try:
                self._loop.run(connection.close(), _CLOSE_WAIT_SECONDS + 5)
            except concurrent.futures.TimeoutError:
                logger.warning("MCP server %s for user %s did not stop in time", key[1], key[0])

    def _key_lock(self, key: tuple[str, str]) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _command(self, server_id: str) -> str:
        server = self.server(server_id)
        command = shutil.which(server.command)
        if command is None:
            raise McpError(
                "The command '{command}' needed by the MCP server '{server}' was not found; install it on the RECALL host.",
                command=server.command, server=server.name,
            )
        return command

    def _environment(self, user_id: str, server_id: str) -> dict[str, str]:
        server = self.server(server_id)
        env: dict[str, str] = {}
        for name, value in server.env.items():
            if isinstance(value, McpEnvFromEnvironment):
                resolved = os.environ.get(value.env, "")
                if not resolved:
                    raise McpError(
                        "Set the environment variable {name}; the MCP server '{server}' needs it.",
                        name=value.env, server=server.name,
                    )
                env[name] = resolved
            elif isinstance(value, McpEnvUserFile):
                env[name] = str((self.state_dir(user_id, server_id) / value.user_file).resolve())
            else:
                env[name] = value
        return env

    def _prepared_state_dir(self, user_id: str, server_id: str) -> Path:
        path = self.state_dir(user_id, server_id)
        path.mkdir(parents=True, exist_ok=True)
        return path.resolve()

    @staticmethod
    def _log_path(user_id: str, server_id: str) -> Path | None:
        run_dir = run_log_dir()
        return run_dir / f"mcp_{server_id}_{user_id}.log" if run_dir is not None else None


def _result_text(result: CallToolResult) -> str:
    """Flatten a tool result's content blocks into text for the model."""
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, TextContent):
            parts.append(block.text)
        elif isinstance(block, EmbeddedResource) and isinstance(block.resource, TextResourceContents):
            parts.append(block.resource.text)
        else:
            parts.append(f"[{block.type} content omitted]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(parts)
