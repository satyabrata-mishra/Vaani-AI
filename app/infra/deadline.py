from __future__ import annotations

import time


class DeadlineExceeded(Exception):
    pass


class Deadline:
    """An absolute end time for one request; every stage asks how much time is left."""

    def __init__(self, seconds: float, clock=time.monotonic):
        self.clock = clock
        self.start = clock()
        self.at = self.start + seconds

    def remaining(self) -> float:
        return max(0.0, self.at - self.clock())

    def expired(self) -> bool:
        return self.clock() >= self.at

    def cap(self, seconds: float) -> float:
        """Timeout for a stage: its own cap, but never past the request deadline."""
        return min(seconds, self.remaining())

    def check(self, what: str):
        if self.expired():
            raise DeadlineExceeded(what)
