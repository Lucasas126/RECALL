"""In-process change notifier for open chats: members of a shared agent see each other's turns at once.

`publish` tells every current subscriber of a chat that something changed (a turn was stored, a reply started or
ended, the chat was renamed or deleted). Nothing is buffered without subscribers, so an unwatched chat costs nothing.
Subscribers get a `SimpleQueue` they read from any thread; the web route turns it into server-sent events.
"""
from __future__ import annotations

import threading
from queue import SimpleQueue

ChatKey = tuple[str, str, str]  # (owner, agent, chat)


class ChatChanges:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: dict[ChatKey, list[SimpleQueue]] = {}

    def subscribe(self, owner: str, agent_id: str, chat_id: str) -> SimpleQueue:
        queue: SimpleQueue = SimpleQueue()
        with self._lock:
            self._subscribers.setdefault((owner, agent_id, chat_id), []).append(queue)
        return queue

    def unsubscribe(self, owner: str, agent_id: str, chat_id: str, queue: SimpleQueue) -> None:
        key = (owner, agent_id, chat_id)
        with self._lock:
            queues = self._subscribers.get(key)
            if queues is None:
                return
            if queue in queues:
                queues.remove(queue)
            if not queues:
                del self._subscribers[key]

    def publish(self, owner: str, agent_id: str, chat_id: str, kind: str) -> None:
        with self._lock:
            queues = list(self._subscribers.get((owner, agent_id, chat_id), ()))
        for queue in queues:
            queue.put(kind)

    def watched(self) -> int:
        """Number of chats with at least one subscriber (tests, diagnostics)."""
        with self._lock:
            return len(self._subscribers)
