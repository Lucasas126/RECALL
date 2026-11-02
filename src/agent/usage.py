from __future__ import annotations

import json


def _est_tokens(chars: int) -> int:
    """Rough token estimate (~4 chars/token); used ONLY as a fallback when the api gives no usage."""
    return max(1, chars // 4) if chars else 0


def _split(shares: dict[str, int], total: int) -> dict[str, int]:
    """Distribute `total` tokens across categories by char share (last key gets the remainder)."""
    total_chars = sum(shares.values()) or 1
    out: dict[str, int] = {}
    running = 0
    keys = list(shares)
    for key in keys[:-1]:
        out[key] = round(total * shares[key] / total_chars)
        running += out[key]
    out[keys[-1]] = max(0, total - running)  # remainder keeps the sum exact
    return out


def _tool_chars(messages: list[dict], tool_specs: list[dict] | None) -> int:
    """Chars the tools add to the prompt: their definitions plus any calls/results in context."""
    total = len(json.dumps(tool_specs)) if tool_specs else 0
    for m in messages:
        if m.get("role") == "tool":
            total += len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {})
            args = fn.get("arguments", "")
            total += len(fn.get("name", "")) + len(args if isinstance(args, str) else json.dumps(args))
    return total


def _chars_by_role(messages: list[dict], tool_specs: list[dict] | None = None) -> dict[str, int]:
    return {
        "system_tokens": sum(len(m.get("content") or "") for m in messages if m["role"] == "system"),
        "user_tokens": sum(len(m.get("content") or "") for m in messages if m["role"] == "user"),
        "agent_tokens": sum(len(m.get("content") or "") for m in messages if m["role"] == "assistant"),
        "tool_tokens": _tool_chars(messages, tool_specs),
    }


def turn_breakdown(context: list[dict], reply: str, reasoning: str,
                   prompt_tokens: int | None, completion_tokens: int | None,
                   tool_specs: list[dict] | None = None) -> tuple[dict, bool]:
    """Split one interaction's real in/out tokens by char-share. Returns (breakdown, approx)."""
    approx = prompt_tokens is None or completion_tokens is None
    in_shares = _chars_by_role(context, tool_specs)
    in_total = prompt_tokens if prompt_tokens is not None else _est_tokens(sum(in_shares.values()))
    out_shares = {"response": len(reply), "thinking": len(reasoning)}
    out_total = completion_tokens if completion_tokens is not None else _est_tokens(sum(out_shares.values()))

    breakdown = {"in": in_total, "out": out_total}
    breakdown.update(_split(in_shares, in_total))
    breakdown.update(_split(out_shares, out_total))
    return breakdown, approx


def context_breakdown(messages: list[dict], in_tokens: int | None,
                      tool_specs: list[dict] | None = None) -> tuple[dict, bool]:
    """Split the current in-memory context's input tokens by char-share. Returns (breakdown, approx)."""
    approx = in_tokens is None
    shares = _chars_by_role(messages, tool_specs)
    total = in_tokens if in_tokens is not None else _est_tokens(sum(shares.values()))
    breakdown = {"in": total, **_split(shares, total)}
    return breakdown, approx


def _pct(tokens: int, max_context: int | None) -> str:
    return f"{tokens / max_context * 100:.1f}% of {max_context}" if max_context else "unknown model context"


def cache_tokens(api_usage: dict | None) -> tuple[int, int] | None:
    """(read, written) prompt-cache tokens from the api usage, or None when the provider reported no breakdown."""
    details = (api_usage or {}).get("prompt_tokens_details")
    if not isinstance(details, dict) or "cached_tokens" not in details:
        return None
    return details["cached_tokens"] or 0, details.get("cache_write_tokens") or 0


def _format_cache(cache: tuple[int, int], prompt_tokens: int) -> str:
    read, written = cache
    share = f" ({read / prompt_tokens * 100:.0f}% of input)" if prompt_tokens else ""
    return f"cache: read={read}{share} write={written}\n"


def format_turn(b: dict, prev: dict | None, approx: bool, max_context: int | None,
                cache: tuple[int, int] | None = None) -> str:
    in_s = f"~in={b['in']}" if approx else f"in={b['in']}"
    out_s = f"~out={b['out']}" if approx else f"out={b['out']}"
    note = "  (approx: no api usage)" if approx else ""
    p = prev or {}
    d_in = b["in"] - p.get("in", 0)
    d_sys = b["system_tokens"] - p.get("system_tokens", 0)
    d_usr = b["user_tokens"] - p.get("user_tokens", 0)
    d_agt = b["agent_tokens"] - p.get("agent_tokens", 0)
    d_tool = b["tool_tokens"] - p.get("tool_tokens", 0)
    cache_line = _format_cache(cache, b["in"]) if cache is not None and not approx else ""
    return (
        f"context: {_pct(b['in'], max_context)} | {in_s} {out_s}{note}\n"
        f"{cache_line}"
        f"  ~ system_tokens={b['system_tokens']} user_tokens={b['user_tokens']} agent_tokens={b['agent_tokens']} tool_tokens={b['tool_tokens']}"
        f"  ·  response={b['response']} thinking={b['thinking']}\n"
        f"turn Δ: in={d_in:+d} out={b['out']}\n"
        f"  ~ system_tokens={d_sys:+d} user_tokens={d_usr:+d} agent_tokens={d_agt:+d} tool_tokens={d_tool:+d}"
        f"  ·  response={b['response']} thinking={b['thinking']}"
    )


def format_context(b: dict, approx: bool, max_context: int | None) -> str:
    in_s = f"~in={b['in']}" if approx else f"in={b['in']}"
    note = "  (approx)" if approx else ""
    return (
        f"context loaded: {_pct(b['in'], max_context)} | {in_s}{note}\n"
        f"  ~ system_tokens={b['system_tokens']} user_tokens={b['user_tokens']} agent_tokens={b['agent_tokens']} tool_tokens={b['tool_tokens']}"
    )



