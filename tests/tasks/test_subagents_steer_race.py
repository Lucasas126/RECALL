"""A steering message that lands exactly as a task ends must be refused, never start a stray worker run."""

import threading
import unittest
from collections import deque
from types import SimpleNamespace

from src.service.tasks import TaskManager


def _live(**overrides):
    values = dict(guard=threading.Lock(), feeds=deque(), pending_feeds=0, closed=False)
    values.update(overrides)
    return SimpleNamespace(**values)


class SteerRaceTests(unittest.TestCase):
    def test_driver_closes_the_run_when_no_feed_is_pending(self):
        live = _live()
        self.assertIsNone(TaskManager._next_feed(live))
        self.assertTrue(live.closed)

    def test_a_queued_feed_is_followed_and_the_run_stays_open(self):
        feed = object()
        live = _live(feeds=deque([feed]), pending_feeds=1)
        self.assertIs(TaskManager._next_feed(live), feed)
        self.assertFalse(live.closed)
        self.assertEqual(live.pending_feeds, 0)


if __name__ == "__main__":
    unittest.main()
