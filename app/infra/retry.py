"""When to try a model call again. Bounded, jittered, deadline-aware, and limited by a shared
retry budget so that a provider-wide problem does not turn into a retry storm."""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.providers.base import (CLIENT, CONNECTION, RATE_LIMITED, SERVER, TIMEOUT_AFTER_FIRST_TOKEN,
                                TIMEOUT_BEFORE_FIRST_TOKEN)

RETRYABLE = {RATE_LIMITED, SERVER, CONNECTION, TIMEOUT_BEFORE_FIRST_TOKEN, TIMEOUT_AFTER_FIRST_TOKEN}


@dataclass
class RetryDecision:
    retry: bool
    wait_s: float = 0.0
    reason: str = ""


@dataclass
class RetryPolicy:
    max_retries: int = 2
    base_s: float = 0.5
    cap_s: float = 8.0
    budget_ratio: float = 0.2       # each first attempt earns 0.2 retries; each retry spends 1
    budget_cap: float = 10.0
    naive: bool = False             # retry everything immediately (for the "retries made it worse" run)
    rng: random.Random = field(default_factory=lambda: random.Random(0))

    def __post_init__(self):
        self.budget = self.budget_cap
        self.retries_granted = 0
        self.retries_denied_budget = 0

    def first_attempt(self):
        self.budget = min(self.budget_cap, self.budget + self.budget_ratio)

    def decide(self, kind: str, limit_kind: str | None, retry_after_s: float | None,
               retries_so_far: int, remaining_s: float, expected_call_s: float) -> RetryDecision:
        if self.naive:
            if kind == CLIENT or retries_so_far >= 3:
                return RetryDecision(False, reason="naive: limit")
            self.retries_granted += 1
            return RetryDecision(True, 0.0, "naive: immediate")
        if kind not in RETRYABLE:
            return RetryDecision(False, reason=f"not retryable: {kind}")
        if kind == RATE_LIMITED and limit_kind in ("RPD", "TPD"):
            return RetryDecision(False, reason="daily limit: waiting cannot help")
        if retries_so_far >= self.max_retries:
            return RetryDecision(False, reason="max retries")
        wait = self.rng.uniform(0, min(self.cap_s, self.base_s * 2 ** retries_so_far))
        if retry_after_s:
            wait = retry_after_s + self.rng.uniform(0, 0.25)  # jitter so waiters do not wake together
        if wait + expected_call_s > remaining_s:
            return RetryDecision(False, wait, "deadline: no time for another attempt")
        if self.budget < 1:
            self.retries_denied_budget += 1
            return RetryDecision(False, wait, "retry budget exhausted")
        self.budget -= 1
        self.retries_granted += 1
        return RetryDecision(True, wait, f"retry {retries_so_far + 1} after {kind}")
