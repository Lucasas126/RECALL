"""A minimal streamable-HTTP MCP server for the MCP tests, protected by a bearer token.

`start(token)` serves it on a free localhost port in a background thread and returns
(url, stop). Requests without `Authorization: Bearer <token>` get 401.
"""

from __future__ import annotations

import logging
import socket
import threading
import time

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations


def _app(tokens: set[str]):
    # MCPServer() calls logging.basicConfig, adding a stderr handler to the root logger of
    # the whole test run; keep the root logger exactly as it was.
    root = logging.getLogger()
    saved = (list(root.handlers), root.level)
    server = MCPServer("fake-http")
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def balance() -> str:
        """Return a fixed balance."""
        return "42.00"

    @server.tool()
    def add_note(text: str) -> str:
        """Store a note."""
        return f"noted {text}"

    @server.tool()
    def fail() -> str:
        """Always fail."""
        raise ValueError("boom")

    inner = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    async def app(scope, receive, send):
        header = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
        if scope["type"] == "http" and header.removeprefix("Bearer ") not in tokens:
            await send({"type": "http.response.start", "status": 401, "headers": [(b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await inner(scope, receive, send)

    return app


def start(token: str):
    """Serve the fake server; returns (url, stop, valid_tokens). Remove a token to revoke it."""
    tokens = {token}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # log_config=None: do not let uvicorn reconfigure the process-wide logging of the test run.
    config = uvicorn.Config(_app(tokens), host="127.0.0.1", port=port, log_config=None, lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("fake MCP http server did not start")
        time.sleep(0.05)

    def stop() -> None:
        server.should_exit = True
        thread.join(10)

    return f"http://127.0.0.1:{port}/mcp", stop, tokens
