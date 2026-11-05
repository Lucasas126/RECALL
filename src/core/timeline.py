"""The order of one assistant reply: thinking, tool steps and text, as the model produced them.

A stored assistant turn keeps `content`, `reasoning` and `actions` as before; `timeline` adds the order:
`[{"type": "reasoning", "text": ...}, {"type": "tool", "index": 0}, {"type": "text", "text": ...}]`.
A tool segment points at `actions[index]` instead of copying it. The web client builds the same list from
the live events (see `timelineStep` in app.js), so a reloaded chat looks like the one that streamed.
"""

from __future__ import annotations

from typing import Optional


class TimelineBuilder:
    """Collects reasoning/text chunks and tool steps into ordered segments.

    Consecutive chunks of one kind merge into one segment; a tool step, or a chunk of the other kind, starts a new one.
    A whitespace-only chunk never opens a segment (it only extends one of its own kind).
    """

    def __init__(self) -> None:
        self.segments: list[dict] = []
        self._tools = 0

    def reasoning(self, chunk: str) -> None:
        self._add("reasoning", chunk)

    def text(self, chunk: str) -> None:
        self._add("text", chunk)

    def tool(self) -> None:
        self.segments.append({"type": "tool", "index": self._tools})
        self._tools += 1

    def _add(self, kind: str, chunk: str) -> None:
        if not isinstance(chunk, str) or not chunk:
            return
        last = self.segments[-1] if self.segments else None
        if last is not None and last["type"] == kind:
            last["text"] += chunk
        elif chunk.strip():
            self.segments.append({"type": kind, "text": chunk})

    def build(self, action_count: int) -> Optional[list[dict]]:
        """The segments to store, or None when there is nothing to order (plain text, no thinking, no tools).

        Actions that ran but were never announced (a reply cut off between tools) are appended as tool steps.
        """
        while self._tools < action_count:
            self.tool()
        segments = []
        for segment in self.segments:
            if segment["type"] == "tool":
                if segment["index"] < action_count:
                    segments.append(dict(segment))
            elif segment["text"].strip():
                text = segment["text"].rstrip() if segment["type"] == "reasoning" else segment["text"]
                segments.append({"type": segment["type"], "text": text})
        if not any(segment["type"] != "text" for segment in segments):
            return None
        return segments
