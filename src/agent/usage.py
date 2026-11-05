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


def _n(value: int) -> str:
    """Thousands with dots (12.345), easier to read than 12345."""
    return f"{value:,}".replace(",", ".")


def _signed(value: int) -> str:
    return ("+" if value >= 0 else "-") + _n(abs(value))


def _pct(tokens: int, max_context: int | None) -> str:
    return f"{tokens / max_context * 100:.1f}%" if max_context else "?"


def _context_line(tokens: int, max_context: int | None, approx: bool) -> str:
    tilde = "~" if approx else ""
    if not max_context:
        return f"context      {tilde}{_n(tokens)} tokens (unknown model context)\n"
    return f"context      {tilde}{_n(tokens)} / {_n(max_context)} tokens ({_pct(tokens, max_context)})\n"


def cache_tokens(api_usage: dict | None) -> tuple[int, int] | None:
    """(read, written) prompt-cache tokens from the api usage, or None when the provider reported no breakdown."""
    details = (api_usage or {}).get("prompt_tokens_details")
    if not isinstance(details, dict) or "cached_tokens" not in details:
        return None
    return details["cached_tokens"] or 0, details.get("cache_write_tokens") or 0


def subscription_note(api_usage: dict | None) -> str:
    """A line about Claude subscription usage (plan allowance, not per-token billing); empty for other providers."""
    if (api_usage or {}).get("provider") != "claude_subscription":
        return ""
    estimate = api_usage.get("list_price_estimate_usd")
    price = f" · list-price estimate ${estimate:.4f} (not a charge)" if isinstance(estimate, (int, float)) else ""
    admission = api_usage.get("admission") or {}
    blocked = f" · blocked extra requests={admission['blocked_requests']}" if admission.get("blocked_requests") else ""
    return f"\nplan         Claude subscription (experimental), counts against the plan limits{price}{blocked}"


def _format_cache(cache: tuple[int, int], prompt_tokens: int) -> str:
    read, written = cache
    share = f" ({read / prompt_tokens * 100:.0f}% of input)" if prompt_tokens else ""
    return f"cache        read {_n(read)}{share} · write {_n(written)}\n"


def _parts(b: dict, sign: bool = False) -> str:
    fmt = _signed if sign else _n
    return (f"system {fmt(b['system_tokens'])} · user {fmt(b['user_tokens'])} · agent {fmt(b['agent_tokens'])}"
            f" · tools {fmt(b['tool_tokens'])}")


def format_turn(b: dict, prev: dict | None, approx: bool, max_context: int | None,
                cache: tuple[int, int] | None = None) -> str:
    """Usage report: the first line is the collapsed summary, the rest the details (the summary is not repeated)."""
    tilde = "~" if approx else ""
    p = prev or {}
    delta = {key: b[key] - p.get(key, 0) for key in ("system_tokens", "user_tokens", "agent_tokens", "tool_tokens")}
    cache_line = _format_cache(cache, b["in"]) if cache is not None and not approx else ""
    return (
        f"context {_pct(b['in'], max_context)} · in {tilde}{_n(b['in'])} · out {tilde}{_n(b['out'])}"
        f"{' (approx: no api usage)' if approx else ''}\n"
        f"{_context_line(b['in'], max_context, approx)}"
        f"{cache_line}"
        f"input        ~ {_parts(b)}\n"
        f"output       text {_n(b['response'])} · thinking {_n(b['thinking'])}\n"
        f"this turn    in {_signed(b['in'] - p.get('in', 0))} · out {_n(b['out'])}\n"
        f"             ~ {_parts(delta, sign=True)}"
    )


def format_context(b: dict, approx: bool, max_context: int | None) -> str:
    tilde = "~" if approx else ""
    return (
        f"context loaded {_pct(b['in'], max_context)} · in {tilde}{_n(b['in'])}{' (approx)' if approx else ''}\n"
        f"{_context_line(b['in'], max_context, approx)}"
        f"input        ~ {_parts(b)}"
    )
