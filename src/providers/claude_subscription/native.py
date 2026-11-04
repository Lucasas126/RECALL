# Adapted from the MIT-licensed Hermes plugin "claude-subscription-directsdk-experimental"
# (Copyright (c) 2026 Nous Research and contributors). See docs/user-guide/claude-subscription.md.
"""Claude Code process plumbing: command lookup, child environment, history frames, request body, process trees."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path

from src.providers.claude_subscription.catalog import lookup, thinking_is_mandatory
from src.providers.provider import ProviderUnavailable

INSTALL_HINT = (
    "Claude Code is not installed (no `claude` on PATH or in the usual install folders). "
    "Install it with `npm install -g @anthropic-ai/claude-code`, or set claude_subscription.command "
    "in user.yaml (or RECALL_CLAUDE_COMMAND) to the executable."
)
LOGGED_OUT_HINT = (
    "Claude Code has no usable login where RECALL runs it. Run `claude auth login` as the account RECALL runs as "
    "(with CLAUDE_CONFIG_DIR set to claude_subscription.claude_config_dir when that is configured), then try again."
)
UPDATE_HINT = "Run `claude update`, then try again."
COMMAND_ENV = "RECALL_CLAUDE_COMMAND"
# MCP server name; Claude Code shows its tools as mcp__recall__<tool>
MCP_SERVER = "recall"
TOOL_PREFIX = f"mcp__{MCP_SERVER}__"

# inherited settings that would move the child off the user's Claude login or to another backend
CONFLICTING_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY")
CONFLICTING_SWITCHES = ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
# telemetry, error reporting and other nonessential traffic (feature flags included) stay off by default
QUIET_TRAFFIC = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1"}
# never wanted from a child RECALL starts: self-updates, feedback uploads and the CLI's own title request
ALWAYS_QUIET = {"DISABLE_AUTOUPDATER": "1", "DISABLE_FEEDBACK_COMMAND": "1", "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"}
CALLER_IDENTITY = {"CLAUDE_CODE_ENTRYPOINT": "recall", "CLAUDE_CODE_DISABLE_ARTIFACT": "1"}
# RECALL owns the loop, memory, instructions, budgets and compaction
OWNED_BY_RECALL = {
    "ENABLE_TOOL_SEARCH": "false", "CLAUDE_CODE_MAX_RETRIES": "0", "DISABLE_AUTO_COMPACT": "1", "DISABLE_COMPACT": "1",
    "CLAUDE_CODE_TOTAL_TOKENS_REMINDER": "off", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
}
_INHERITED_OVERRIDES = ("CLAUDE_CODE_EXTRA_BODY", "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDECODE")
_HOME_PREFIXES = (".local/bin", ".claude/local", "bin", ".npm-global/bin", ".bun/bin", ".volta/bin")
_SYSTEM_PREFIXES = ("/opt/homebrew/bin", "/usr/local/bin")
_EFFORTS = {"minimal": "low", "low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "max"}
_COMBINATORS = ("oneOf", "allOf", "anyOf")


class ClaudeCodeMissing(ProviderUnavailable):
    """The Claude Code CLI is not installed or not found."""


class ClaudeCodeLoggedOut(ProviderUnavailable):
    """Claude Code refused before any upstream request because it has no usable login."""


class ClaudeCodeTooOld(RuntimeError):
    """The installed Claude Code does not support the requested model."""


class ClaudeSubscriptionError(RuntimeError):
    """A failed Claude Code run or upstream answer."""


def resolve_command(command: list[str] | None, env: dict[str, str]) -> list[str] | None:
    """The executable (absolute) plus arguments, or None when Claude Code cannot be found."""
    command = list(command) if command else [env.get(COMMAND_ENV) or "claude"]
    head = command[0]
    if os.path.isabs(head):
        exe = head if os.path.isfile(head) else None
    else:
        exe = shutil.which(head, path=env.get("PATH") or os.defpath)
    if not exe and head == "claude":
        # services start with a minimal PATH; only the default name is looked up in the usual folders
        home = env.get("USERPROFILE" if os.name == "nt" else "HOME")
        prefixes = [os.path.join(home, prefix) for prefix in _HOME_PREFIXES] if home else []
        prefixes += [] if os.name == "nt" else list(_SYSTEM_PREFIXES)
        exe = shutil.which(head, path=os.pathsep.join(prefixes))
    return [exe, *command[1:]] if exe else None


def child_env(source: dict[str, str], *, config_dir: str | None, telemetry: bool) -> dict[str, str]:
    """The environment for one Claude Code run; refuses inherited API-key or custom-backend overrides."""
    conflicts = [key for key in CONFLICTING_ENV if source.get(key)]
    conflicts += [key for key in CONFLICTING_SWITCHES if source.get(key, "").lower() not in ("", "0", "false", "no", "off")]
    if conflicts:
        raise ClaudeSubscriptionError(
            "The Claude subscription provider uses your Claude Code login only; remove these environment "
            "variables from RECALL's environment: " + ", ".join(conflicts)
        )
    env = dict(source)
    for key in _INHERITED_OVERRIDES:
        env.pop(key, None)
    _with_login_identity(env)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    env.update(OWNED_BY_RECALL)
    env.update(ALWAYS_QUIET)
    env.update(CALLER_IDENTITY)
    if not telemetry:
        env.update(QUIET_TRAFFIC)
    return env


def _with_login_identity(env: dict[str, str]) -> None:
    """Claude Code's local login check reads the user name; services can start without one."""
    if os.name == "nt":
        name = env.get("USERNAME") or env.get("USER") or ""
        if name:
            env.setdefault("USERNAME", name)
            env.setdefault("USER", name)
        return
    name = env.get("USER") or ""
    if not name:
        try:
            import pwd
            name = pwd.getpwuid(os.getuid()).pw_name
        except (ImportError, KeyError):
            name = ""
    if name:
        env.setdefault("USER", name)
        env.setdefault("LOGNAME", name)


def workdir() -> str:
    """A stable private cwd for Claude Code, so its environment message does not change between runs."""
    owner = str(os.getuid()) if hasattr(os, "getuid") else (os.environ.get("USERNAME") or "user")
    path = Path(tempfile.gettempdir()) / f"recall-claude-cwd-{re.sub(r'[^A-Za-z0-9_-]', '_', owner)}"
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        if hasattr(os, "getuid"):
            info = path.lstat()
            if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return tempfile.mkdtemp(prefix="recall-claude-cwd-")
    except OSError:
        return tempfile.mkdtemp(prefix="recall-claude-cwd-")
    return str(path)


def own_process_group() -> dict:
    """Popen kwargs that put Claude Code and its children (node behind the npm shim) in one killable group."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(process: subprocess.Popen) -> None:
    """Kill Claude Code and every descendant; Popen.kill() alone would orphan the node child of the shim."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def session_id(system: str, frames: list[dict]) -> str:
    """A session id that stays the same for every request of one conversation (stable prompt prefix)."""
    first = frames[0]["message"] if frames else None
    digest = hashlib.sha256(json.dumps([system, first], sort_keys=True, ensure_ascii=False).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=digest[:16], version=4))


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return "" if content is None else str(content)


def content_blocks(content) -> list[dict]:
    """Anthropic content blocks for an OpenAI-style message content; unsupported media become a text note."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else None
        if kind == "text":
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
        elif kind == "image_url":
            url = part["image_url"]["url"]
            if url.startswith("data:") and ";base64," in url:
                media, data = url[5:].split(";base64,", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
            else:
                blocks.append({"type": "text", "text": "[Image not attached: only inline images are supported.]"})
        else:
            blocks.append({"type": "text", "text": f"[{kind or 'unknown'} attachment omitted: not supported by this model.]"})
    return blocks


def _tool_input(arguments) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        value = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def prepare_history(
    messages: list[dict], names: dict[str, str], native_turns: dict[tuple, dict],
) -> tuple[str, list[dict]]:
    """Split RECALL's messages into a system prompt and Claude Code stream-json frames.

    Leading system messages form the system prompt; later ones (memory hints, the final-hop nudge) become
    user text. An assistant tool-call turn this provider produced is replayed as Claude sent it, signed
    thinking included, which the API requires while a tool loop is open.
    """
    system: list[str] = []
    frames: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role in ("system", "developer"):
            if not frames:
                system.append(_text(message.get("content")))
                continue
            role, blocks = "user", [{"type": "text", "text": f"[System] {_text(message.get('content'))}"}]
        elif role == "assistant":
            calls = message.get("tool_calls") or []
            native = native_turns.get(tuple(call["id"] for call in calls)) if calls else None
            if native is not None and _text(native["content"]).strip() == _text(message.get("content")).strip():
                blocks = [dict(block) for block in native["content"]]
            else:
                blocks = content_blocks(message.get("content"))
                for call in calls:
                    function = call.get("function") or {}
                    name = function.get("name", "")
                    blocks.append({
                        "type": "tool_use", "id": call["id"], "name": TOOL_PREFIX + names.get(name, name),
                        "input": _tool_input(function.get("arguments")),
                    })
            if not blocks:
                continue
        elif role == "tool":
            content = message.get("content")
            role, blocks = "user", [{
                "type": "tool_result", "tool_use_id": message["tool_call_id"],
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            }]
        elif role == "user":
            blocks = content_blocks(message.get("content"))
            if not blocks:
                continue
        else:
            raise ValueError(f"unsupported message role for Claude Code: {role!r}")
        if frames and frames[-1]["type"] == role:
            frames[-1]["message"]["content"].extend(blocks)
        else:
            frames.append({"type": role, "message": {"role": role, "content": blocks}})
    if not frames or frames[-1]["type"] != "user":
        raise ValueError("the conversation must end with a user or tool message for Claude Code")
    if frames[0]["type"] != "user":
        frames.insert(0, {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "[Earlier conversation omitted]"}]}})
    return "\n\n".join(part for part in system if part), frames


def _input_schema(schema) -> dict:
    """Anthropic rejects top-level oneOf/allOf/anyOf; tools re-validate their own arguments anyway."""
    schema = dict(schema) if isinstance(schema, dict) else {"type": "object"}
    if any(key in schema for key in _COMBINATORS):
        schema = {k: v for k, v in schema.items() if k not in _COMBINATORS}
        schema.setdefault("type", "object")
    if schema.get("type") == "object" and not isinstance(schema.get("properties"), dict):
        schema["properties"] = {}
    return schema


def request_body(
    model: str, *, reasoning: bool, reasoning_effort: str | None, tools: list[dict] | None,
) -> tuple[dict, list[dict], dict[str, str]]:
    """Generation fields for CLAUDE_CODE_EXTRA_BODY, the inert MCP manifest, and RECALL -> Claude tool names."""
    body: dict[str, object] = {}
    entry = lookup(model)
    canonical = entry.canonical if entry else model
    if reasoning:
        if not canonical.startswith("claude-haiku-4-5"):
            body["thinking"] = {"type": "adaptive", "display": "summarized"}
        if reasoning_effort:
            body["output_config"] = {"effort": _EFFORTS.get(reasoning_effort, "medium")}
    elif thinking_is_mandatory(model):
        body["output_config"] = {"effort": "low"}  # thinking cannot be turned off; the lowest effort is closest
    else:
        body["thinking"] = {"type": "disabled"}
        body["context_management"] = {"edits": []}
    manifest, specs, names, taken = [], [], {}, {}
    for tool in tools or []:
        function = tool.get("function") or {}
        name = function.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in names:
            raise ValueError(f"tool name {name!r} must be a unique ASCII identifier of at most 64 characters")
        native = name if len(name) <= 50 else name[:41] + "_" + hashlib.sha256(name.encode()).hexdigest()[:8]
        if native in taken:
            raise ValueError(f"tool names {name!r} and {taken[native]!r} collide for Claude Code")
        names[name], taken[native] = native, name
        schema, description = _input_schema(function.get("parameters")), function.get("description") or ""
        manifest.append({"name": native, "description": description, "inputSchema": schema})
        specs.append({"name": TOOL_PREFIX + native, "description": description, "input_schema": schema})
    body["tools"] = specs
    return body, manifest, names


def stderr_tail(lines) -> str:
    text = " ".join(line.strip() for line in lines if line.strip())
    return f" (Claude Code stderr: {text[:400]})" if text else ""
