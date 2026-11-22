# Adapted from the MIT-licensed Hermes plugin "claude-subscription-directsdk-experimental"
# (Copyright (c) 2026 Nous Research and contributors). See docs/user-guide/claude-subscription.md.
# Ported from the plugin's admission relay: stable request context, message cache breakpoint pinning
# (with the long-tool-loop turn mark) and restoration of the queried tool-result frame.
"""Prompt-cache hygiene for the Messages request Claude Code builds, applied by the admission relay.

Claude Code adds per-request context to the turn it answers (today's date split out of its environment
message, an account-context reminder before the newest user text) and puts its message cache breakpoint
on or after that context. The next request replays the turn without it, so the cached prefix never
recurs and every call writes the whole history again. These transforms keep the request equal to what
the next request replays and the breakpoint on content that recurs. Every function returns the payload
unchanged when it does not parse or has an unexpected shape, so a CLI change costs cache, never a turn.
The account-context reminder (a `<system-reminder>` with a `# userEmail` section) is also removed from the
newest message when the tool-result frame could not be restored, never anything else in a tool result.
"""
from __future__ import annotations

import copy
import json
import re

UNCACHEABLE = ("thinking", "redacted_thinking")
# Anthropic's cache lookup walks back at most this many blocks from a breakpoint and allows this many
# breakpoints across tools, system and messages
LOOKBACK = 20
MAX_BREAKPOINTS = 4
# the only trailing system text moved back into the environment message: the date line as the opening
# request ends that message; other trailing text stays put
_DATE_LINE = re.compile(r"Today's date is \d{4}-\d{2}-\d{2}\.")


class QueriedTurnMismatch(ValueError):
    """The request could not be reconciled with the tool results RECALL sent."""


def _dumps(body: dict) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _plain(block):
    return {k: v for k, v in block.items() if k != "cache_control"} if isinstance(block, dict) else block


def _breakpoints(value) -> int:
    """Every `cache_control` in `value`, nested ones (tool_result content) included."""
    count, pending = 0, [value]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            count += "cache_control" in node
            pending += (v for k, v in node.items() if k != "cache_control")
        elif isinstance(node, list):
            pending += node
    return count


def _frame_first(messages: list[dict], queried: list[dict]) -> bool:
    """Send the conversation's first user turn without the context Claude Code prepends to it.

    Later requests replay that turn without it, so keeping it made request 2 write the turn again, and
    Claude would read Claude Code's notes as part of the user's message. Only the first turn (no
    assistant message yet), only when the queried frame occurs exactly once and everything before it is
    plain text. A cache mark on a dropped block moves onto the frame without multiplying.
    """
    if any(m.get("role") == "assistant" for m in messages):
        return False
    first = next((m for m in messages if m.get("role") == "user"), {})
    content = first.get("content")
    if not isinstance(content, list) or not queried or len(content) <= len(queried):
        return False
    want = [_plain(b) for b in queried]
    starts = [k for k in range(len(content) - len(queried) + 1)
              if [_plain(b) for b in content[k:k + len(queried)]] == want]
    if len(starts) != 1 or starts[0] == 0:
        return False
    k = starts[0]
    if not all(isinstance(b, dict) and b.get("type") == "text" and set(b) <= {"type", "text", "cache_control"}
               for b in content[:k]):
        return False
    mark = next((b["cache_control"] for b in content[:k] if "cache_control" in b), None)
    del content[:k]
    if mark is not None:
        content[0].setdefault("cache_control", mark)
    return True


def stable_request_context(payload: bytes, queried: list[dict] | None) -> bytes:
    """Keep Claude Code's per-request context where later requests replay it.

    The date line split into a trailing system message goes back to the end of the environment message;
    plain text before the whole queried frame (found exactly once) of a later user turn is dropped, as
    Claude Code already does on tool-result steps. Removed cache marks move without multiplying.
    """
    try:
        body = json.loads(payload)
        messages = body["messages"]
        if not isinstance(messages, list) or not all(isinstance(m, dict) for m in messages):
            return payload
        for message in messages:
            content = message["content"]
            if message.get("role") not in ("system", "user", "assistant") or not isinstance(content, (str, list)):
                return payload
            if isinstance(content, list) and any(
                    not isinstance(b, dict) or not isinstance(b.get("type"), str)
                    or (b["type"] == "text" and not isinstance(b.get("text"), str)) for b in content):
                return payload
        if queried is not None and (not isinstance(queried, list) or any(not isinstance(b, dict) for b in queried)):
            return payload
        changed = False
        last = messages[-1] if messages else {}
        env = next((m for m in messages[:-1] if m.get("role") == "system"), None)
        content = last.get("content")
        tail = {"type": "text", "text": content} if isinstance(content, str) else (
            content[0] if isinstance(content, list) and len(content) == 1 else {})
        if last.get("role") == "system" and env is not None and tail.get("type") == "text" \
                and _DATE_LINE.fullmatch(tail.get("text", "")):
            original, line = env["content"], "\n\n" + tail["text"]
            target = {"type": "text", "text": original} if isinstance(original, str) else (original[-1] if original else {})
            if target.get("type") == "text" and not target["text"].endswith(tail["text"]):
                if isinstance(original, str):
                    env["content"] = original + line
                else:
                    target["text"] += line
                mark = tail.get("cache_control", last.get("cache_control"))
                messages.pop()
                previous = messages[-1]["content"]
                if mark is not None and isinstance(previous, list) and previous:
                    previous[-1].setdefault("cache_control", mark)
                changed = True
        newest = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        blocks = newest.get("content")
        if queried and any(b.get("type") != "tool_result" for b in queried) and isinstance(blocks, list) \
                and any(m.get("role") == "assistant" for m in messages):
            frame = [_plain(b) for b in queried]
            hits = [i for i in range(len(blocks) - len(frame) + 1) if [_plain(b) for b in blocks[i:i + len(frame)]] == frame]
            k = hits[0] if len(hits) == 1 else None
            if k and all(b.get("type") == "text" and set(b) <= {"type", "text", "cache_control"} for b in blocks[:k]):
                mark = next((b["cache_control"] for b in blocks[:k] if "cache_control" in b), None)
                del blocks[:k]
                if mark is not None:
                    blocks[0].setdefault("cache_control", mark)
                changed = True
        return _dumps(body) if changed else payload
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return payload


def _opens_turn(message: dict) -> bool:
    """A user message carrying anything but tool results: a new turn, or a steering message after them."""
    content = message.get("content")
    return message.get("role") == "user" and (isinstance(content, str) or any(
        not isinstance(b, dict) or b.get("type") != "tool_result" for b in content or []))


def _keep_turn_mark(body: dict, messages: list[dict], last: int, target: tuple) -> bool:
    """After a tool loop longer than the lookback, keep a mark where the open turn's thinking starts.

    Once the next turn arrives, entries written after the turn's first thinking block stop matching, and
    the recurring breakpoint cannot reach further back than LOOKBACK blocks, so the next turn would read
    only tools and system. The extra mark goes on the last cacheable block of the user message right
    before the turn's first assistant message with thinking, takes the TTL of the next mark after it, and
    is added only within MAX_BREAKPOINTS (at the limit a mark between it and the recurring one moves there).
    """
    opening = max((i for i in range(last) if _opens_turn(messages[i])), default=None)
    if opening is None:
        return False
    thought = next((i for i in range(opening + 1, last + 1) if messages[i].get("role") == "assistant"
                    and isinstance(messages[i].get("content"), list)
                    and any(isinstance(b, dict) and b.get("type") in UNCACHEABLE for b in messages[i]["content"])), None)
    if thought is None:
        return False
    frame = next((i for i in range(thought - 1, opening, -1) if messages[i].get("role") == "user"), opening)
    content = messages[frame].get("content")
    if not isinstance(content, list):
        return False
    j = next((k for k in reversed(range(len(content)))
              if isinstance(content[k], dict) and content[k].get("type") not in UNCACHEABLE), None)
    if j is None or "cache_control" in content[j] or (frame, j) >= target[:2]:
        return False
    span = sum(len(m["content"]) if isinstance(m.get("content"), list) else 1 for m in messages[frame:target[0]])
    if span - j + target[1] + 1 <= LOOKBACK:
        return False  # the recurring breakpoint's own lookback still reaches it
    marks = [(i, k, b) for i, m in enumerate(messages) if isinstance(m.get("content"), list)
             for k, b in enumerate(m["content"]) if isinstance(b, dict) and "cache_control" in b]
    later = [(i, k, b) for i, k, b in marks if (frame, j) < (i, k)]
    if not later:
        return False
    if _breakpoints(body) < MAX_BREAKPOINTS:
        content[j]["cache_control"] = copy.deepcopy(later[0][2]["cache_control"])
        return True
    spare = next((b for i, k, b in later if (i, k) < target[:2]), None)
    if spare is None or "cache_control" not in target[2]:
        return False
    content[j]["cache_control"] = spare.pop("cache_control")
    return True


def pin_message_breakpoint(payload: bytes, queried: list[dict] | None) -> bytes:
    """Keep the recurring message `cache_control` on content the next request replays unchanged.

    What recurs is everything through the last assistant message plus the leading blocks of the newest
    turn that equal the frame RECALL queried; the first block Claude Code added or changed ends that span
    (when that block is a tool_result the span ends at the preceding assistant message). Marks after the
    span fold into one on its last block; marks inside it, and every system/tools mark, stay. The count
    grows by at most the turn mark of `_keep_turn_mark` and never past four.
    """
    if not queried:
        return payload
    try:
        body = json.loads(payload)
        messages = body["messages"]
        reordered = _frame_first(messages, queried)
        blocks = [(i, j, b) for i, m in enumerate(messages) if isinstance(m.get("content"), list)
                  for j, b in enumerate(m["content"])]
        marked = [(i, j, b) for i, j, b in blocks if isinstance(b, dict) and "cache_control" in b]
        if not marked and not reordered:
            return payload
        last = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        stable = [(i, j, b) for i, j, b in blocks if i <= last]
        newest = messages[last + 1] if last + 1 < len(messages) else {}
        if newest.get("role") == "user" and isinstance(newest.get("content"), list):
            prefix = []
            for j, (sent, host) in enumerate(zip(newest["content"], queried)):
                if _plain(sent) != _plain(host):
                    break
                prefix.append((last + 1, j, sent))
            rest = newest["content"][len(prefix):]
            if not (rest and isinstance(rest[0], dict) and rest[0].get("type") == "tool_result"):
                stable += prefix
        target = next(((i, j, b) for i, j, b in reversed(stable)
                       if isinstance(b, dict) and b.get("type") not in UNCACHEABLE), None)
        after = [b for i, j, b in marked if target is not None and (target[0], target[1]) < (i, j)]
        folded = target is not None and bool(after)
        if folded and target is not None:
            moved = [b.pop("cache_control") for b in after][0]
            target[2].setdefault("cache_control", moved)
        turn = target is not None and _keep_turn_mark(body, messages, last, target)
        if not (reordered or folded or turn):
            return payload
        return _dumps(body)
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return payload


def restore_queried_turn(payload: bytes, queried: list[dict] | None) -> bytes:
    """Restore the newest tool-result frame exactly as RECALL sent it when it matches unambiguously.

    Claude Code may append its context to tool results; restored, the frame equals what the next request
    replays, and Claude never reads Claude Code's notes as tool output. Only trailing bare text is
    removable; anything else that does not match raises QueriedTurnMismatch (the caller forwards the
    payload unchanged). Ordinary user frames are outside its scope and pass through byte for byte.
    """
    if not isinstance(queried, list) or not queried:
        return payload
    if not any(isinstance(block, dict) and block.get("type") == "tool_result" for block in queried):
        return payload

    def unmatched():
        raise QueriedTurnMismatch("the request does not uniquely match RECALL's tool results")

    def bare_text(block) -> bool:
        return (isinstance(block, dict) and set(block) == {"type", "text"}
                and block["type"] == "text" and isinstance(block["text"], str))

    def restore_content(sent, host):
        # scalar/list equivalence only for text with no metadata or markers
        if isinstance(host, str) or isinstance(sent, str):
            def text(value):
                if isinstance(value, str):
                    return value
                if isinstance(value, list) and all(bare_text(b) for b in value):
                    return "".join(b["text"] for b in value)
                return None
            left, right = text(sent), text(host)
            if left is None or right is None or not left.startswith(right):
                return unmatched()
            return copy.deepcopy(host)
        if isinstance(sent, list) and isinstance(host, list):
            return restore_sequence(sent, host)
        if sent != host:
            return unmatched()
        return copy.deepcopy(host)

    def restore_block(sent, host):
        if not isinstance(sent, dict) or not isinstance(host, dict):
            return unmatched()
        restored = copy.deepcopy(host)
        if host.get("type") == "tool_result":
            left_error, right_error = sent.get("is_error", False), host.get("is_error", False)
            if type(left_error) is not bool or type(right_error) is not bool or left_error != right_error:
                return unmatched()
            ignored = {"content", "is_error", "cache_control"}
            if {k: v for k, v in sent.items() if k not in ignored} != {k: v for k, v in host.items() if k not in ignored}:
                return unmatched()
            if ("content" in sent) != ("content" in host):
                return unmatched()
            if "content" in host:
                restored["content"] = restore_content(sent["content"], host["content"])
        elif _plain(sent) != _plain(host):
            return unmatched()
        if "cache_control" in sent:
            if "cache_control" in host and sent["cache_control"] != host["cache_control"]:
                return unmatched()
            restored["cache_control"] = copy.deepcopy(sent["cache_control"])
        return restored

    def restore_sequence(sent, host):
        restored = []
        i = j = 0
        while i < len(host):
            if j >= len(sent):
                return unmatched()
            if bare_text(host[i]) and bare_text(sent[j]):
                host_end, sent_end = i + 1, j + 1
                while host_end < len(host) and bare_text(host[host_end]):
                    host_end += 1
                while sent_end < len(sent) and bare_text(sent[sent_end]):
                    sent_end += 1
                original = "".join(b["text"] for b in host[i:host_end])
                actual = "".join(b["text"] for b in sent[j:sent_end])
                # extensions before a protected host block are insertions, not suffixes
                if not (actual.startswith(original) if host_end == len(host) else actual == original):
                    return unmatched()
                restored.extend(copy.deepcopy(host[i:host_end]))
                i, j = host_end, sent_end
            else:
                restored.append(restore_block(sent[j], host[i]))
                i += 1
                j += 1
        if any(not bare_text(b) for b in sent[j:]):
            return unmatched()
        return restored

    try:
        body = json.loads(payload)
        messages = body["messages"]
        last_assistant = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
        newest = messages[last_assistant + 1] if last_assistant + 1 < len(messages) else {}
        sent_blocks = newest.get("content") if newest.get("role") == "user" else None
        if not isinstance(sent_blocks, list):
            return unmatched()
        result_ids = [block.get("tool_use_id") for block in queried
                      if isinstance(block, dict) and block.get("type") == "tool_result"]
        if any(not isinstance(rid, str) or not rid for rid in result_ids) or len(set(result_ids)) != len(result_ids):
            return unmatched()
        sent_ids = [b.get("tool_use_id") for b in sent_blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if result_ids != sent_ids:
            return unmatched()
        restored = restore_sequence(sent_blocks, queried)
        if sent_blocks == restored:
            return payload
        newest["content"] = restored
        return _dumps(body)
    except QueriedTurnMismatch:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return unmatched()


_REMINDER_OPEN, _REMINDER_CLOSE = "<system-reminder>", "</system-reminder>"


def _is_account_reminder(text) -> bool:
    """Exactly one system-reminder block with a `# userEmail` line (other reminders are not ours)."""
    if not isinstance(text, str):
        return False
    body = text.strip()
    return (body.startswith(_REMINDER_OPEN) and body.endswith(_REMINDER_CLOSE)
            and body.count(_REMINDER_OPEN) == 1 and body.count(_REMINDER_CLOSE) == 1
            and any(line.strip() == "# userEmail" for line in body.splitlines()))


def strip_account_reminder(payload: bytes) -> bytes:
    """Remove the account-context reminder Claude Code appends to the newest user message.

    Only text Claude Code added is touched: bare text blocks of the newest message, and the trailing text
    block (or trailing suffix of a string) of a tool result when that is exactly one such reminder. A
    removed cache mark moves to the previous block of the same list. Any other shape returns the payload
    unchanged, byte for byte.
    """
    def bare_text(block) -> bool:
        return (isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
                and set(block) <= {"type", "text", "cache_control"})

    def drop_trailing(blocks: list, index: int) -> bool:
        # remove blocks[index] (a reminder), keeping its cache mark on the block before it
        mark = blocks[index].get("cache_control")
        if mark is not None:
            if index == 0 or not isinstance(blocks[index - 1], dict):
                return False
            blocks[index - 1].setdefault("cache_control", mark)
        del blocks[index]
        return True

    try:
        body = json.loads(payload)
        messages = body.get("messages") if isinstance(body, dict) else None
        if not isinstance(messages, list) or not messages:
            return payload
        newest = messages[-1]
        if not isinstance(newest, dict) or newest.get("role") != "user" or not isinstance(newest.get("content"), list):
            return payload
        content = newest["content"]
        if not all(isinstance(b, dict) for b in content):
            return payload
        changed = False
        # tool results first, while the indexes of the outer list are still stable
        for block in content:
            if block.get("type") != "tool_result":
                continue
            inner = block.get("content")
            if isinstance(inner, str):
                start = inner.rfind(_REMINDER_OPEN)
                if start > 0 and _is_account_reminder(inner[start:]) and inner[:start].strip():
                    block["content"] = inner[:start]
                    changed = True
            elif isinstance(inner, list) and len(inner) > 1 and all(bare_text(b) for b in inner):
                if _is_account_reminder(inner[-1]["text"]) and drop_trailing(inner, len(inner) - 1):
                    changed = True
        for index in range(len(content) - 1, -1, -1):
            if bare_text(content[index]) and _is_account_reminder(content[index]["text"]):
                if len(content) > 1 and drop_trailing(content, index):
                    changed = True
        return _dumps(body) if changed else payload
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        return payload


def cache_ready(payload: bytes, queried: list[dict] | None) -> tuple[bytes, str | None]:
    """All transforms in the relay's order; returns the payload and why tool results were not restored."""
    unrestored = None
    try:
        payload = restore_queried_turn(payload, queried)
    except QueriedTurnMismatch as exc:
        # hygiene, not a gate: an unprovable frame forwards as Claude Code built it (an uncached tail)
        unrestored = str(exc)
    payload = strip_account_reminder(payload)
    payload = stable_request_context(payload, queried)
    payload = pin_message_breakpoint(payload, queried)
    return payload, unrestored
