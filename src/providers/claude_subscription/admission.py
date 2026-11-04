# Adapted from the MIT-licensed Hermes plugin "claude-subscription-directsdk-experimental"
# (Copyright (c) 2026 Nous Research and contributors). See docs/claude-subscription.md.
# Kept: single-request admission, SSE capture, HTTP CONNECT proxy support, socket cancellation and the
# prompt-cache hygiene of `cache.py`. Left out: request gzip and eager tool-input streaming.
"""Request-scoped loopback relay that forwards only the first Messages request of one Claude Code run.

Claude Code can start extra generations on its own (recovery, retries) even with `--max-turns 1`; each
would spend subscription allowance on output RECALL never reads. The relay forwards the first
`/v1/messages` request, captures its streamed answer, and refuses every later one locally. Before forwarding, the request goes
through `cache.cache_ready` so its prompt-cache prefix recurs on the next request. Authorization headers
pass through memory to the upstream and are never logged or stored.
"""
from __future__ import annotations

import base64
import codecs
import copy
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import SplitResult, unquote, urlsplit

from src.providers.claude_subscription.cache import cache_ready
from src.utils.logger import get_logger

logger = get_logger("provider-claude-subscription")

CONSUMED = b'{"type":"error","error":{"type":"invalid_request_error","message":"RECALL_ADMISSION_CONSUMED"}}'
# longest silence from the upstream after its headers before the stream counts as dead
UPSTREAM_IDLE_SECONDS = 180
# Winsock: shutdown() does not wake a recv blocked in another thread; closing the handle does
_CANCEL_BY_CLOSE = os.name == "nt"


class Capture:
    """Rebuilds the assistant message from the upstream SSE stream as it passes through."""

    def __init__(self) -> None:
        self.message: dict | None = None
        self.complete = False
        self._pending = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._arguments: dict[int, str] = {}
        self._unparsed: list[int] = []

    def feed(self, chunk: bytes) -> None:
        self._pending += self._decoder.decode(chunk)
        while match := re.search(r"\r?\n\r?\n", self._pending):
            frame, self._pending = self._pending[:match.start()], self._pending[match.end():]
            data = "\n".join(line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:"))
            if data:
                self._event(json.loads(data))

    def _event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "message_start":
            self.message = copy.deepcopy(event["message"])
        elif kind == "content_block_start":
            self.message["content"].append(copy.deepcopy(event["content_block"]))
        elif kind == "content_block_delta":
            delta = event["delta"]
            block = self.message["content"][event["index"]]
            field = {"text_delta": "text", "thinking_delta": "thinking", "signature_delta": "signature"}.get(delta["type"])
            if field:
                block[field] = block.get(field, "") + delta[field]
            elif delta["type"] == "input_json_delta":
                self._arguments[event["index"]] = self._arguments.get(event["index"], "") + delta["partial_json"]
        elif kind == "content_block_stop":
            index = event["index"]
            if index in self._arguments:
                raw = self._arguments.pop(index)
                try:
                    self.message["content"][index]["input"] = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    # a reply cut off at max_tokens ends mid-argument: the call is dropped, never guessed
                    self._unparsed.append(index)
        elif kind == "message_delta":
            self.message.update(event.get("delta", {}))
            self.message.setdefault("usage", {}).update(event.get("usage", {}))
        elif kind == "message_stop":
            for index in sorted(self._unparsed, reverse=True):
                del self.message["content"][index]
            self.complete = bool(self.message and self.message.get("stop_reason") and not self._arguments)


def upstream_proxy(upstream: SplitResult) -> tuple[str, int, dict | None] | None:
    """`(host, port, tunnel_headers)` of the HTTP CONNECT proxy for the upstream, or None (standard proxy variables)."""
    if upstream.scheme != "https":
        return None
    proxies = urllib.request.getproxies_environment()
    if "no" in proxies:
        proxies["no"] = proxies["no"].replace(" ", ",")
    if urllib.request.proxy_bypass_environment(f"{upstream.hostname}:{upstream.port or 443}", proxies):
        return None
    raw = proxies.get("https") or proxies.get("http")
    if not raw:
        return None
    proxy = urlsplit(raw if "://" in raw else "http://" + raw)
    try:
        port = proxy.port or 80
    except ValueError:
        port = None
    if proxy.scheme != "http" or not proxy.hostname or port is None:
        # never quote the URL: it may carry credentials
        logger.warning("ignoring HTTPS_PROXY for the Claude relay: only an http://host[:port] CONNECT proxy is supported")
        return None
    headers = None
    if proxy.username is not None:
        credentials = unquote(proxy.username) + ":" + unquote(proxy.password or "")
        headers = {"Proxy-Authorization": "Basic " + base64.b64encode(credentials.encode()).decode()}
    return proxy.hostname, port, headers


class _Server(HTTPServer):
    """Quiet about clients that hang up early (Claude Code's warm-up probe does on every run)."""

    def handle_error(self, request, client_address) -> None:
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class Admission:
    """One loopback listener on a random route; admits the first Messages request, refuses the rest."""

    def __init__(self, upstream: str, timeout: float, queried: list[dict] | None = None) -> None:
        self.upstream = urlsplit(upstream)
        self.queried = queried  # content of the frame RECALL queried; anchors the cache transforms
        self.unrestored: str | None = None
        host = self.upstream.hostname
        try:
            local = host is not None and ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == "localhost"
        if (self.upstream.scheme != "https" and not (self.upstream.scheme == "http" and local)) or not host \
                or self.upstream.username or self.upstream.password or self.upstream.query or self.upstream.fragment:
            raise ValueError("the Claude upstream must be HTTPS (or a loopback HTTP test fixture)")
        self.timeout = timeout
        self.proxy = upstream_proxy(self.upstream)
        self.lock = threading.Lock()
        self.sockets: set[socket.socket] = set()
        self.cancelled = False
        self.used = False
        self.denied = 0
        self.status: int | None = None
        self.failure: str | None = None
        self.capture = Capture()
        self.error_body = b""
        self.prefix = "/admit/" + secrets.token_urlsafe(32)
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.admission = self
        self.url = f"http://127.0.0.1:{self.server.server_port}{self.prefix}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def error_text(self) -> str:
        """The upstream's own message for a non-200 answer; empty when none was captured."""
        text = self.error_body.decode("utf-8", errors="replace")
        try:
            message = json.loads(text)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            return text
        return message if isinstance(message, str) else text

    def abort(self) -> None:
        with self.lock:
            self.cancelled = True
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                if _CANCEL_BY_CLOSE:
                    try:
                        fd = sock.detach()
                        if fd is not None and fd >= 0:
                            socket.close(fd)
                    except OSError:
                        pass

    def close(self) -> None:
        self.abort()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class _Handler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        gate = self.server.admission
        with gate.lock:
            gate.sockets.add(self.request)
            cancelled = gate.cancelled
        try:
            self.request.settimeout(gate.timeout)
            if cancelled:
                self.request.shutdown(socket.SHUT_RDWR)
            super().setup()
        except BaseException:
            with gate.lock:
                gate.sockets.discard(self.request)
            raise

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            with self.server.admission.lock:
                self.server.admission.sockets.discard(self.request)

    def log_message(self, format, *args) -> None:  # noqa: A002 (base-class signature)
        pass  # the authorization header and the per-run route must never reach logs

    def _connect(self):
        gate, target = self.server.admission, self.server.admission.upstream
        if target.scheme == "https":
            if gate.proxy:
                host, port, headers = gate.proxy
                conn = http.client.HTTPSConnection(host, port, timeout=gate.timeout, context=ssl.create_default_context())
                conn.set_tunnel(target.hostname, target.port or 443, headers=headers)
            else:
                conn = http.client.HTTPSConnection(target.hostname, target.port, timeout=gate.timeout, context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=gate.timeout)
        try:
            conn.connect()
            with gate.lock:
                if gate.cancelled:
                    conn.close()
                    return None, None
                gate.sockets.add(conn.sock)
            return conn, conn.sock
        except BaseException:
            conn.close()
            raise

    def do_POST(self) -> None:
        gate = self.server.admission
        path = urlsplit(self.path)
        if path.path != gate.prefix + "/v1/messages" or self.headers.get("Origin"):
            self.send_error(404)
            return
        with gate.lock:
            if gate.cancelled or gate.used:
                gate.denied += 1
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(CONSUMED)))
                self.end_headers()
                self.wfile.write(CONSUMED)
                return
            gate.used = True
        conn = upstream_socket = None
        try:
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            payload, gate.unrestored = cache_ready(payload, gate.queried)
            conn, upstream_socket = self._connect()
            if conn is None:
                return
            headers = {
                k: v for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "content-length", "transfer-encoding",
                                     "proxy-authorization", "proxy-connection", "accept-encoding")
            }
            headers["Accept-Encoding"] = "identity"
            route = gate.upstream.path.rstrip("/") + "/v1/messages" + ("?" + path.query if path.query else "")
            conn.request("POST", route, payload, headers)
            del headers, payload
            response = conn.getresponse()
            gate.status = response.status
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() not in ("connection", "transfer-encoding", "server", "date"):
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if not response.isclosed():
                upstream_socket.settimeout(min(gate.timeout, UPSTREAM_IDLE_SECONDS))
            while chunk := response.read1(65536):
                if response.status == 200:
                    gate.capture.feed(chunk)
                elif len(gate.error_body) < 65536:
                    gate.error_body += chunk
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError) as exc:
            gate.failure = type(exc).__name__
        finally:
            with gate.lock:
                gate.sockets.discard(upstream_socket)
            if conn:
                conn.close()
            self.close_connection = True
