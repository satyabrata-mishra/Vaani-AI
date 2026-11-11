"""Our own view of the provider's budget, so we wait or refuse *before* sending instead of
collecting 429s. Local accounting is primary; provider headers only correct it downwards,
because under concurrency they arrive out of order (R4)."""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.infra.token_bucket import TokenBucket
from app.providers.base import CallResult, RATE_LIMITED


class DailyBudgetExhausted(Exception):
    def __init__(self, kind: str, reset_s: float | None):
        super().__init__(kind)
        self.kind = kind
        self.reset_s = reset_s


@dataclass
class LimiterConfig:
    rpm: int
    tpm: int
    rpd: int
    tpd: int
    rpm_burst: int | None = None
    day_used_requests: int = 0
    day_used_tokens: int = 0


class ClientRateLimiter:
    def __init__(self, cfg: LimiterConfig, clock=time.monotonic):
        self.cfg = cfg
        self.clock = clock
        self.rpm = TokenBucket(cfg.rpm_burst or cfg.rpm, cfg.rpm / 60.0, clock)
        self.tpm = TokenBucket(cfg.tpm, cfg.tpm / 60.0, clock)
        self.day_requests = cfg.day_used_requests
        self.day_tokens = cfg.day_used_tokens
        self.day_remaining_requests_hdr: int | None = None
        self.day_reset_at: float | None = None   # clock() time the provider said the daily budget resets
        self.daily_blocked: str | None = None
        self.cooldown_until = 0.0
        self.observed_tpm_limit: int | None = None

    # --- planning --------------------------------------------------------------------------
    @property
    def day_reset_s(self) -> float | None:
        """Seconds until the provider's reported daily reset, from now."""
        return None if self.day_reset_at is None else max(0.0, self.day_reset_at - self.clock())

    def daily_check(self, est_tokens: int):
        if self.daily_blocked:
            raise DailyBudgetExhausted(self.daily_blocked, self.day_reset_s if self.daily_blocked == "RPD" else None)
        req_left = self.cfg.rpd - self.day_requests
        if self.day_remaining_requests_hdr is not None:
            req_left = min(req_left, self.day_remaining_requests_hdr)
        if req_left < 1:
            raise DailyBudgetExhausted("RPD", self.day_reset_s)
        if self.day_tokens + est_tokens > self.cfg.tpd:
            raise DailyBudgetExhausted("TPD", None)

    def wait_needed(self, est_tokens: int) -> float:
        now = self.clock()
        return max(self.cooldown_until - now, self.rpm.seconds_until(1),
                   self.tpm.seconds_until(min(est_tokens, self.tpm.capacity)))

    def tpm_headroom(self) -> float:
        """Fraction of the per-minute token budget available right now (can be < 0)."""
        return self.tpm.available() / self.tpm.capacity

    def reserve(self, est_tokens: int):
        self.rpm.charge(1)
        self.tpm.charge(est_tokens)
        self.day_requests += 1
        self.day_tokens += est_tokens

    def settle(self, est_tokens: int, actual_tokens: int | None):
        """Replace the estimate with the real usage once the call is over."""
        if actual_tokens is None:
            return
        diff = actual_tokens - est_tokens
        self.tpm.charge(diff)
        self.day_tokens += diff

    # --- learning from responses ------------------------------------------------------------
    def observe(self, r: CallResult):
        rl = r.rate_limit
        if rl.limit_tokens_minute and rl.limit_tokens_minute != self.cfg.tpm:
            # the provider changed the per-minute token limit (e.g. halved): follow it
            self.observed_tpm_limit = rl.limit_tokens_minute
            self.cfg.tpm = rl.limit_tokens_minute
            self.tpm.capacity = rl.limit_tokens_minute
            self.tpm.per_second = rl.limit_tokens_minute / 60.0
        if rl.remaining_tokens_minute is not None:
            avail = self.tpm.available()
            if rl.remaining_tokens_minute < avail:
                self.tpm.charge(avail - rl.remaining_tokens_minute)
        if rl.remaining_requests_day is not None:
            self.day_remaining_requests_hdr = rl.remaining_requests_day
        if rl.reset_requests_day_s is not None:
            self.day_reset_at = self.clock() + rl.reset_requests_day_s
        if r.error_kind == RATE_LIMITED:
            if r.limit_kind in ("RPD", "TPD"):
                self.daily_blocked = r.limit_kind
            elif rl.retry_after_s:
                self.cooldown_until = max(self.cooldown_until, self.clock() + rl.retry_after_s)
            if r.limit_kind == "RPM":
                # our bucket was too optimistic: empty it so we pace at the refill rate
                self.rpm.charge(max(0.0, self.rpm.available()))
