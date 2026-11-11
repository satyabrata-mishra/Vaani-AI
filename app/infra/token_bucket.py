from __future__ import annotations

import math
import time


class TokenBucket:
    """Continuously refilling bucket. `level` may go negative (debt) when actual usage is charged
    after admission, which is how Groq's remaining-tokens header behaved."""

    def __init__(self, capacity: float, per_second: float, clock=time.monotonic):
        self.capacity = capacity
        self.per_second = per_second
        self.clock = clock
        self.level = capacity
        self.t = clock()

    def _refill(self):
        now = self.clock()
        self.level = min(self.capacity, self.level + (now - self.t) * self.per_second)
        self.t = now

    def available(self) -> float:
        self._refill()
        return self.level

    def charge(self, n: float):
        self._refill()
        self.level -= n

    def seconds_until(self, n: float) -> float:
        self._refill()
        if self.level >= n:
            return 0.0
        if self.per_second <= 0:
            return math.inf
        return (n - self.level) / self.per_second
