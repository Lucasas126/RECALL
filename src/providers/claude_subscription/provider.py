# Adapted from the MIT-licensed Hermes plugin "claude-subscription-directsdk-experimental"
# (Copyright (c) 2026 Nous Research and contributors). See docs/user-guide/claude-subscription.md.
"""Experimental provider that drives the official Claude Code CLI with the user's own Claude login."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import ClassVar, Iterator

from pydantic import PrivateAttr

from src.config.model_refs import claude_model
from src.providers.cancellation import CancellationToken
from src.providers.claude_subscription import native
from src.providers.claude_subscription.admission import Admission
from src.providers.claude_subscription.catalog import context_window, lookup, native_model, parse_version
from src.providers.claude_subscription.native import (
    ClaudeCodeLoggedOut, ClaudeCodeMissing, ClaudeCodeTooOld, ClaudeSubscriptionError,
)
from src.providers.provider import LLMProvider, format_completion
from src.utils.logger import get_logger

logger = get_logger("provider-claude-subscription")

_INERT_MCP = Path(__file__).with_name("inert_mcp.py")
# native assistant turns remembered for replay with their signed thinking (per provider instance)
_NATIVE_TURNS = 64
# seconds Claude Code gets to exit after its result before its process tree is killed
_EXIT_GRACE = 2.0
_LOGGED_OUT_MARKERS = ("not logged in", "please run /login", "invalid api key", "oauth token has expired")


class ClaudeSubscriptionProvider(LLMProvider):
    """One Claude Code process per request: RECALL keeps the loop and executes tools; Claude Code only answers."""

    name: ClassVar[str] = "claude_subscription"

    command: list[str] | None = None  # executable and leading arguments; None = RECALL_CLAUDE_COMMAND, then `claude`
    config_dir: str | None = None  # CLAUDE_CONFIG_DIR of the user's own login; None = the CLI default
    telemetry: bool = False  # False starts Claude Code with telemetry, error reporting and nonessential traffic off
    timeout_seconds: float = 300  # longest silence from Claude Code before the request fails
    upstream_url: str = "https://api.anthropic.com"

    _native_turns: OrderedDict = PrivateAttr(default_factory=OrderedDict)
    _cli_version: tuple | None = PrivateAttr(default=None)
    _state_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def context_window(self, model: str):
        return context_window(model)

    def supported_modalities(self, model: str) -> set[str]:
        return {"text", "image"}

    def invoke_embeddings(self, input: list[str], model: str, cancellation: CancellationToken | None = None):
        raise ClaudeSubscriptionError(
            "the Claude subscription provider cannot compute embeddings; keep embeddings on OpenRouter"
        )

    def invoke(self, messages: list[dict], model: str, stream=True, reasoning=False, reasoning_tokens=None, reasoning_effort=None, tools=None, cancellation: CancellationToken | None = None, *, zdr: bool = False):
        """Stream (kind, text) tuples then a final dict, like the other providers; `zdr` does not apply here."""
        events = self._run(messages, model, reasoning=reasoning, reasoning_effort=reasoning_effort, tools=tools, cancellation=cancellation)
        if stream:
            return events
        final = None
        for event in events:
            if isinstance(event, dict):
                final = event
        return final

    # ------------------------------------------------------------------ setup checks

    def _environment(self) -> tuple[list[str], dict[str, str]]:
        env = native.child_env(dict(os.environ), config_dir=self.config_dir, telemetry=self.telemetry)
        resolved = native.resolve_command(self.command, env)
        if resolved is None:
            raise ClaudeCodeMissing(native.INSTALL_HINT)
        return resolved, env

    def _check_version(self, command: list[str], env: dict[str, str], model: str) -> None:
        """Fail early with `claude update` when the CLI is older than the model needs."""
        entry = lookup(model)
        if entry is None:
            return
        with self._state_lock:
            version = self._cli_version
        if version is None:
            try:
                run = subprocess.run(
                    [*command, "--version"], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, encoding="utf-8", errors="replace", timeout=30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ClaudeCodeMissing(f"{native.INSTALL_HINT} ({type(exc).__name__}: {exc})") from exc
            version = parse_version(run.stdout) or ()
            with self._state_lock:
                self._cli_version = version
        if version and version < entry.min_cli:
            raise ClaudeCodeTooOld(
                f"Claude Code {'.'.join(map(str, version))} does not support {entry.name}; it needs "
                f"{'.'.join(map(str, entry.min_cli))} or newer. {native.UPDATE_HINT}"
            )

    def _remember_native(self, ids: tuple, message: dict) -> None:
        with self._state_lock:
            self._native_turns[ids] = message
            self._native_turns.move_to_end(ids)
            while len(self._native_turns) > _NATIVE_TURNS:
                self._native_turns.popitem(last=False)

    # ------------------------------------------------------------------ one request

    def _run(
        self, messages: list[dict], model: str, *, reasoning: bool, reasoning_effort: str | None,
        tools: list[dict] | None, cancellation: CancellationToken | None,
    ) -> Iterator[tuple[str, str] | dict]:
        model_name = claude_model(model)
        if not model_name:
            raise ValueError("a Claude subscription model name is required")
        logger.debug("request model=%s reasoning=%s msgs=%d tools=%d", model_name, reasoning, len(messages), len(tools or []))
        body, manifest, names = native.request_body(model_name, reasoning=reasoning, reasoning_effort=reasoning_effort, tools=tools)
        recall_names = {claude: recall for recall, claude in names.items()}
        with self._state_lock:
            turns = dict(self._native_turns)
        system, frames = native.prepare_history(messages, names, turns)
        command, env = self._environment()
        self._check_version(command, env, model_name)
        if cancellation is not None:
            cancellation.raise_if_cancelled()
        process: subprocess.Popen | None = None
        admission: Admission | None = None
        threads: list[threading.Thread] = []
        stderr: deque[str] = deque(maxlen=40)
        with tempfile.TemporaryDirectory(prefix="recall-claude-", ignore_cleanup_errors=True) as tmp:
            root = Path(tmp)
            try:
                (root / "tools.json").write_text(json.dumps(manifest), encoding="utf-8")
                (root / "mcp.json").write_text(json.dumps({"mcpServers": {native.MCP_SERVER: {
                    "command": sys.executable, "args": ["-I", str(_INERT_MCP), str(root / "tools.json")],
                }}}), encoding="utf-8")
                # full tool schemas and generation fields go through a settings file, not argv or env limits
                (root / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_EXTRA_BODY": json.dumps(body)}}), encoding="utf-8")
                # newline="" keeps the prompt byte-identical (no CRLF translation on Windows)
                (root / "system.md").write_text(system, encoding="utf-8", newline="")
                # the queried frame lets the relay keep the cache breakpoint off Claude Code's per-request context
                admission = Admission(self.upstream_url, self.timeout_seconds, queried=frames[-1]["message"]["content"])
                env["ANTHROPIC_BASE_URL"] = admission.url
                argv = [
                    *command, "-p", "--model", native_model(model_name),
                    "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                    "--include-partial-messages", "--tools", "", "--system-prompt-file", str(root / "system.md"),
                    "--settings", str(root / "settings.json"), "--setting-sources", "", "--strict-mcp-config",
                    "--mcp-config", str(root / "mcp.json"), "--disable-slash-commands", "--max-turns", "1",
                    "--permission-mode", "dontAsk", "--no-session-persistence",
                    "--session-id", native.session_id(system, frames),
                ]
                if effort := body.get("output_config", {}).get("effort"):
                    argv += ["--effort", effort]
                process = subprocess.Popen(
                    argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    encoding="utf-8", errors="replace", cwd=native.workdir(), env=env, **native.own_process_group(),
                )
                events: queue.Queue = queue.Queue()
                threads = [
                    threading.Thread(target=_read_events, args=(process, events), daemon=True),
                    threading.Thread(target=_drain, args=(process, stderr), daemon=True),
                ]
                for thread in threads:
                    thread.start()
                yield from self._exchange(process, events, frames, admission, stderr, model, recall_names, cancellation)
            finally:
                if process is not None:
                    _finish(process)
                if admission is not None:
                    admission.close()
                for thread in threads:
                    thread.join(timeout=5)
                if process is not None:
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        try:
                            if pipe and not pipe.closed:
                                pipe.close()
                        except OSError:
                            pass

    def _exchange(
        self, process: subprocess.Popen, events: queue.Queue, frames: list[dict], admission: Admission,
        stderr: deque, model: str, recall_names: dict[str, str], cancellation: CancellationToken | None,
    ) -> Iterator[tuple[str, str] | dict]:
        timeout = self.timeout_seconds

        def receive() -> dict | None:
            deadline = time.monotonic() + timeout
            while True:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Claude Code sent nothing for {timeout:g} seconds")
                try:
                    event = events.get(timeout=min(remaining, 0.2))
                except queue.Empty:
                    continue
                if isinstance(event, Exception):
                    raise ClaudeSubscriptionError(f"invalid stream-json output from Claude Code: {event}{native.stderr_tail(stderr)}")
                return event

        def send(line: str | None) -> None:
            try:
                if line is None:
                    process.stdin.close()
                else:
                    process.stdin.write(line)
                    process.stdin.flush()
            except OSError as exc:
                raise ClaudeSubscriptionError(f"Claude Code stopped reading its input{native.stderr_tail(stderr)}") from exc

        # history frames are replayed without a query; each is acknowledged with a zero-turn result
        for index, frame in enumerate(frames):
            last = index == len(frames) - 1
            send(json.dumps({**frame, **({} if last or frame["type"] != "user" else {"shouldQuery": False})}) + "\n")
            if frame["type"] == "user" and not last:
                while True:
                    ack = receive()
                    if ack is None:
                        raise ClaudeSubscriptionError(f"Claude Code exited during history replay (exit={process.poll()}){native.stderr_tail(stderr)}")
                    if ack.get("type") == "result":
                        if ack.get("num_turns") != 0 or ack.get("is_error"):
                            raise ClaudeSubscriptionError("this Claude Code version does not support history replay; " + native.UPDATE_HINT)
                        break
        send(None)

        assistants: list[dict] = []
        result: dict | None = None
        native_error = native_code = None
        emitted, reasoned = "", ""
        while True:
            event = receive()
            if event is None:
                break
            kind = event.get("type")
            if kind == "stream_event":
                delta = event.get("event", {}).get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    emitted += delta["text"]
                    yield ("content", delta["text"])
                elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    reasoned += delta["thinking"]
                    yield ("reasoning", delta["thinking"])
            elif kind == "assistant":
                message = event.get("message") or {}
                if event.get("error") or message.get("error"):
                    native_error = _text_of(message.get("content") or [])
                    native_code = event.get("error") or message.get("error")
                else:
                    assistants.append(message)
            elif kind == "result":
                result = event
                break

        message = self._answer(admission, assistants, result, native_error, native_code, stderr)
        blocks = message.get("content") or []
        text = _text_of(blocks)
        if text != emitted:
            if text.startswith(emitted):
                yield ("content", text[len(emitted):])
            else:
                raise ClaudeSubscriptionError("Claude Code's final text differs from what it streamed")
        thinking = "".join(block.get("thinking", "") for block in blocks if block.get("type") == "thinking")
        if thinking and not reasoned:
            yield ("reasoning", thinking)
        calls = []
        for block in blocks:
            if block.get("type") == "tool_use":
                name = block["name"].removeprefix(native.TOOL_PREFIX)
                calls.append({
                    "id": block["id"], "name": recall_names.get(name, name),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                })
        if calls:
            replay = {k: v for k, v in message.items() if k in ("role", "content")}
            replay.setdefault("role", "assistant")
            self._remember_native(tuple(call["id"] for call in calls), replay)
        usage = _usage(message.get("usage") or (result or {}).get("usage"), result, admission)
        logger.debug(format_completion("claude-subscription", thinking or reasoned, text, usage, model))
        yield {"reasoning": thinking or reasoned, "response": text, "usage": usage, "model": model, "tool_calls": calls or None}

    @staticmethod
    def _answer(
        admission: Admission, assistants: list[dict], result: dict | None, native_error: str | None,
        native_code: str | None, stderr: deque,
    ) -> dict:
        """The completed assistant message, or the most actionable error for what went wrong."""
        if admission.used:
            if admission.status == 200 and admission.capture.complete:
                return admission.capture.message  # the first upstream answer is authoritative
            detail = admission.error_text() or native_error or admission.failure or "incomplete response"
            if admission.status == 401:
                raise ClaudeCodeLoggedOut(f"{native.LOGGED_OUT_HINT} (upstream: {detail[:300]})")
            if "does not support this model" in detail:
                raise ClaudeCodeTooOld(f"{detail[:300]} {native.UPDATE_HINT}")
            if admission.status == 429:
                raise ClaudeSubscriptionError(f"Claude plan limit or rate limit reached: {detail[:300]}")
            raise ClaudeSubscriptionError(f"Claude request failed (status {admission.status}): {detail[:500]}")
        result_text = str((result or {}).get("result") or "")
        problem = native_error or (result_text if (result or {}).get("is_error") else "")
        if native_code == "authentication_failed" or any(marker in problem.lower() for marker in _LOGGED_OUT_MARKERS):
            raise ClaudeCodeLoggedOut(f"{native.LOGGED_OUT_HINT} (Claude Code: {problem[:300]}){native.stderr_tail(stderr)}")
        if "does not support this model" in problem:
            raise ClaudeCodeTooOld(f"{problem[:300]} {native.UPDATE_HINT}")
        if native_error:
            prefix = "Claude plan limit reached" if native_code == "rate_limit" else "Claude Code error"
            raise ClaudeSubscriptionError(f"{prefix}: {native_error[:500]}")
        if result is None:
            raise ClaudeSubscriptionError(f"Claude Code exited without an answer{native.stderr_tail(stderr)}")
        has_calls = any(block.get("type") == "tool_use" for a in assistants for block in a.get("content") or [])
        boundary = has_calls and result.get("subtype") == "error_max_turns"
        if not boundary and (result.get("is_error") or result.get("subtype") != "success"):
            raise ClaudeSubscriptionError(f"Claude Code request failed: {result.get('subtype')} {result_text[:300]}".rstrip())
        if not assistants:
            raise ClaudeSubscriptionError("Claude Code returned no assistant message")
        merged = {k: v for k, v in assistants[-1].items() if k != "content"}
        merged["content"] = [block for a in assistants for block in a.get("content") or []]
        return merged


def _text_of(blocks: list) -> str:
    return "".join(block.get("text", "") for block in blocks if isinstance(block, dict) and block.get("type") == "text")


def _usage(native_usage: dict | None, result: dict | None, admission: Admission) -> dict | None:
    """OpenAI-style usage with Anthropic cache fields; the CLI's cost is a list-price estimate, not a charge."""
    if not isinstance(native_usage, dict) or not isinstance(native_usage.get("input_tokens"), int):
        return None
    read = native_usage.get("cache_read_input_tokens") or 0
    written = native_usage.get("cache_creation_input_tokens") or 0
    prompt = native_usage["input_tokens"] + read + written
    completion = native_usage.get("output_tokens") or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": read, "cache_write_tokens": written},
        "provider": "claude_subscription",
        "list_price_estimate_usd": (result or {}).get("total_cost_usd"),
        "admission": {
            "upstream_requests": int(admission.used), "blocked_requests": admission.denied,
            **({"unrestored": admission.unrestored} if admission.unrestored else {}),
        },
    }


def _read_events(process: subprocess.Popen, events: queue.Queue) -> None:
    try:
        for line in process.stdout:
            if line.strip():
                events.put(json.loads(line))
    except Exception as exc:  # handed to the consumer, which raises it with context
        events.put(exc)
    finally:
        events.put(None)


def _drain(process: subprocess.Popen, lines: deque) -> None:
    try:
        for line in process.stderr:
            lines.append(line)
    except (OSError, ValueError):
        pass


def _finish(process: subprocess.Popen) -> None:
    """Let Claude Code exit after its answer, else kill its whole tree (also on cancellation and errors)."""
    try:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=_EXIT_GRACE)
    except subprocess.TimeoutExpired:
        native.kill_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("Claude Code process %s did not exit after kill", process.pid)
