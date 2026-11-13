"""Plan usage of one Claude subscription login (the 5-hour session and the weekly window), as a percentage.

Reads the OAuth access token Claude Code stored in the login folder and asks Anthropic's usage endpoint, the
same one Claude Code itself uses. The token is only sent to api.anthropic.com and never returned or logged: the
result carries percentages and reset times only. An expired token is not refreshed here (Claude Code refreshes
it on its next run); the answer is then `expired`.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# window key in the endpoint's answer -> label shown to the user
WINDOWS = (
    ("five_hour", "session"), ("seven_day", "week"), ("seven_day_opus", "week_opus"), ("seven_day_sonnet", "week_sonnet"),
)


def credentials_file(config_dir: str | None) -> Path:
    base = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    return Path(base).expanduser() / ".credentials.json"


def _token(config_dir: str | None) -> tuple[str | None, str | None]:
    """(access token, reason it is missing): reason is `no_credentials` or `expired`."""
    try:
        oauth = json.loads(credentials_file(config_dir).read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (OSError, ValueError, AttributeError):
        return None, "no_credentials"
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        return None, "no_credentials"
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires / 1000 <= time.time():
        return None, "expired"
    return token, None


def parse_usage(payload: dict) -> list[dict]:
    """The windows of the endpoint's answer that exist: [{key, percent, resets_at}], percent clamped to 0-100."""
    rows = []
    for key, name in WINDOWS:
        window = payload.get(key)
        if not isinstance(window, dict) or not isinstance(window.get("utilization"), (int, float)):
            continue
        resets = window.get("resets_at")
        rows.append({
            "key": name, "percent": max(0.0, min(100.0, float(window["utilization"]))),
            "resets_at": resets if isinstance(resets, str) else None,
        })
    return rows


def fetch_usage(config_dir: str | None, *, timeout: float = 10.0) -> dict:
    """`{available: True, windows: [...]}` or `{available: False, reason}` (no_credentials, expired, unauthorized, unreachable)."""
    token, reason = _token(config_dir)
    if token is None:
        return {"available": False, "reason": reason}
    headers = {
        "Authorization": f"Bearer {token}", "Accept": "application/json", "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    try:
        response = httpx.get(USAGE_URL, headers=headers, timeout=timeout)
    except httpx.HTTPError:
        return {"available": False, "reason": "unreachable"}
    if response.status_code in (401, 403):
        return {"available": False, "reason": "unauthorized"}
    if response.status_code != 200:
        return {"available": False, "reason": "unreachable"}
    try:
        windows = parse_usage(response.json())
    except ValueError:
        return {"available": False, "reason": "unreachable"}
    return {"available": True, "windows": windows}
