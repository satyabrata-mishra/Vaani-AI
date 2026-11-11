"""The gate in front of every model call: bounded FIFO queue, rate-limit pacing, concurrency cap.

A request is turned away *at arrival* when the queue is full or when the estimated wait already
exceeds its deadline, instead of waiting and failing late."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.infra.deadline import Deadline, DeadlineExceeded
from app.infra.rate_limiter import ClientRateLimiter


class AdmissionRejected(Exception):
    def __init__(self, reason: str, estimated_wait_s: float | None = None):
        super().__init__(reason)
        self.reason = reason                 # "queue_full" | "wait_exceeds_deadline"
        self.estimated_wait_s = estimated_wait_s


@dataclass
class Permit:
    gate: "ModelGate"
    est_tokens: int
    queue_wait_s: float
    queue_len_at_arrival: int
    released: bool = False

    def release(self, actual_tokens: int | None):
        if self.released:
            return
        self.released = True
        self.gate.limiter.settle(self.est_tokens, actual_tokens)
        self.gate.in_flight -= 1
        self.gate.sem.release()


class ModelGate:
    def __init__(self, limiter: ClientRateLimiter, max_in_flight: int, queue_max: int,
                 expected_service_s: float = 1.0, clock=time.monotonic):
        self.limiter = limiter
        self.max_in_flight = max_in_flight
        self.queue_max = queue_max
        self.expected_service_s = expected_service_s
        self.clock = clock
        self.sem = asyncio.Semaphore(max_in_flight)
        self.fifo = asyncio.Lock()          # asyncio.Lock wakes waiters in FIFO order
        self.waiting = 0
        self.queued_tokens = 0
        self.in_flight = 0
        self.peak_waiting = 0
        self.peak_in_flight = 0

    def estimate_wait(self, est_tokens: int) -> float:
        L = self.limiter
        tok = max(0.0, (self.queued_tokens + est_tokens - L.tpm.available()) / L.tpm.per_second)
        req = max(0.0, (self.waiting + 1 - L.rpm.available()) / L.rpm.per_second)
        cool = max(0.0, L.cooldown_until - self.clock())
        slots = (self.waiting + self.in_flight) // self.max_in_flight * self.expected_service_s
        return max(tok, req, cool, slots)

    async def acquire(self, est_tokens: int, deadline: Deadline) -> Permit:
        self.limiter.daily_check(est_tokens)
        arrival_len = self.waiting
        if self.waiting >= self.queue_max:
            raise AdmissionRejected("queue_full")
        est = self.estimate_wait(est_tokens)
        if est + self.expected_service_s > deadline.remaining():
            raise AdmissionRejected("wait_exceeds_deadline", est)
        self.waiting += 1
        self.queued_tokens += est_tokens
        self.peak_waiting = max(self.peak_waiting, self.waiting)
        t0 = self.clock()
        try:
            async with self.fifo:
                while (w := self.limiter.wait_needed(est_tokens)) > 0:
                    if w >= deadline.remaining():
                        raise DeadlineExceeded("deadline passed while waiting for rate limit")
                    await asyncio.sleep(w)
                try:
                    await asyncio.wait_for(self.sem.acquire(), deadline.remaining())
                except asyncio.TimeoutError:
                    raise DeadlineExceeded("deadline passed while waiting for a model slot") from None
                self.limiter.daily_check(est_tokens)  # may have changed while we waited
                self.limiter.reserve(est_tokens)
        except BaseException:
            self.waiting -= 1
            self.queued_tokens -= est_tokens
            raise
        self.waiting -= 1
        self.queued_tokens -= est_tokens
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        return Permit(self, est_tokens, self.clock() - t0, arrival_len)
