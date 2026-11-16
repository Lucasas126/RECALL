"""Scripted stand-in for the `claude` CLI: reads stream-json frames and answers with scripted stream-json.

Usage: python fake_claude.py --scenario <name> <claude arguments...>
FAKE_CLAUDE_RECORD names a JSON file that receives the arguments, selected environment and input frames.
FAKE_CLAUDE_PIDFILE names a file that receives the pids of the `hang` scenario's process tree.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

RECORDED_ENV = (
    "DISABLE_TELEMETRY", "DISABLE_ERROR_REPORTING", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_AUTOUPDATER",
    "CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_MAX_RETRIES", "USERNAME", "USER",
)
USAGE = {"input_tokens": 12, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7}


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def stream_text(text: str, kind: str = "text") -> None:
    for piece in (text[: len(text) // 2], text[len(text) // 2:]):
        delta = {"type": "text_delta", "text": piece} if kind == "text" else {"type": "thinking_delta", "thinking": piece}
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": delta}})


def answer(content: list[dict], *, subtype: str = "success", stop: str = "end_turn") -> None:
    emit({"type": "stream_event", "event": {"type": "message_stop"}})
    emit({"type": "assistant", "message": {"id": "msg_fake", "role": "assistant", "content": content, "stop_reason": stop, "usage": USAGE}})
    emit({"type": "result", "subtype": subtype, "is_error": subtype != "success" and stop != "tool_use",
          "num_turns": 1, "result": "", "usage": USAGE, "total_cost_usd": 0.0012})


def main() -> None:
    args = sys.argv[1:]
    scenario = "reply"
    if args[:1] == ["--scenario"]:
        scenario, args = args[1], args[2:]
    if args == ["--version"]:
        print(os.environ.get("FAKE_CLAUDE_VERSION", "2.1.316 (Claude Code)"))
        return
    frames = []
    for line in sys.stdin:
        frame = json.loads(line)
        frames.append(frame)
        if frame.get("shouldQuery") is False:
            emit({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "result": ""})
    if record := os.environ.get("FAKE_CLAUDE_RECORD"):
        with open(args[args.index("--system-prompt-file") + 1], "rb") as fh:
            system_bytes = fh.read()
        with open(record, "w", encoding="utf-8") as fh:
            json.dump({"argv": args, "env": {k: os.environ.get(k) for k in RECORDED_ENV}, "frames": frames,
                       "system_prompt": system_bytes.decode("utf-8")}, fh)
    last = frames[-1]["message"]["content"]
    if scenario == "reply":
        stream_text("Hello world")
        answer([{"type": "text", "text": "Hello world"}])
    elif scenario == "thinking":
        stream_text("Let me think", kind="thinking")
        stream_text("Done")
        answer([{"type": "thinking", "thinking": "Let me think", "signature": "sig"}, {"type": "text", "text": "Done"}])
    elif scenario == "tool":
        results = [block for block in last if block.get("type") == "tool_result"]
        if results:
            text = f"The tool said {results[0]['content']}"
            stream_text(text)
            answer([{"type": "text", "text": text}])
        else:
            answer([
                {"type": "thinking", "thinking": "I need the time", "signature": "sig-1"},
                {"type": "tool_use", "id": "toolu_1", "name": "mcp__recall__get_time", "input": {"zone": "UTC"}},
            ], subtype="error_max_turns", stop="tool_use")
    elif scenario == "error":
        emit({"type": "assistant", "error": "rate_limit", "message": {"role": "assistant", "content": [{"type": "text", "text": "You've hit your session limit"}]}})
        emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "result": "You've hit your session limit"})
    elif scenario == "logged_out":
        emit({"type": "assistant", "error": "authentication_failed", "message": {"role": "assistant", "content": [{"type": "text", "text": "Not logged in · Please run /login"}]}})
        emit({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "result": "Not logged in · Please run /login"})
    elif scenario == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(os.environ["FAKE_CLAUDE_PIDFILE"], "w", encoding="utf-8") as fh:
            json.dump([os.getpid(), child.pid], fh)
        stream_text("partial")
        time.sleep(120)
    elif scenario == "native_cache":
        # posts the request the way Claude Code shapes it: a reminder before the newest user text, the date
        # split into a trailing system message that carries the message cache mark
        mark = {"type": "ephemeral", "ttl": "1h"}
        messages = [{"role": f["type"], "content": [dict(b) for b in f["message"]["content"]]} for f in frames]
        messages[-1]["content"].insert(0, {"type": "text", "text": "<system-reminder>context</system-reminder>"})
        if len(messages) == 1:  # the opening request keeps the date in the environment message
            messages.append({"role": "system", "content": [
                {"type": "text", "text": "Environment.\n\nToday's date is 2026-01-02.", "cache_control": mark}]})
        else:
            messages.insert(1, {"role": "system", "content": "Environment."})
            messages.append({"role": "system", "content": [{"type": "text", "text": "Today's date is 2026-01-02.", "cache_control": mark}]})
        body = json.dumps({"model": "fake", "system": [{"type": "text", "text": "system", "cache_control": mark}],
                           "messages": messages}).encode()
        request = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages", data=body,
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read()
        stream_text("Relayed answer")
        answer([{"type": "text", "text": "Relayed answer"}])
    elif scenario == "relay":
        url = os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages"
        statuses = []
        for _ in range(2):  # a second generation attempt must be refused by the relay
            request = urllib.request.Request(url, data=b'{"model":"fake"}', headers={"Content-Type": "application/json", "Authorization": "Bearer fake"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    response.read()
                    statuses.append(response.status)
            except urllib.error.HTTPError as exc:
                statuses.append(exc.code)
        stream_text("Relayed answer")
        # the native transcript differs on purpose: the captured upstream answer must win
        answer([{"type": "text", "text": "Relayed answer"}])
        print(json.dumps({"type": "system", "subtype": "fake_statuses", "statuses": statuses}), file=sys.stderr, flush=True)
    else:
        raise SystemExit(f"unknown scenario {scenario}")


if __name__ == "__main__":
    main()
