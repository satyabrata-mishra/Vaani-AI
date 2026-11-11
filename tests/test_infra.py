import asyncio
import csv
import random

import pytest

from app.config import RESULTS_COLUMNS
from app.infra.admission import AdmissionRejected, ModelGate
from app.infra.deadline import Deadline
from app.infra.metrics import RequestTrace, ResultsWriter
from app.infra.rate_limiter import ClientRateLimiter, DailyBudgetExhausted, LimiterConfig
from app.infra.retry import RetryPolicy
from app.providers.base import CLIENT, RATE_LIMITED, SERVER, CallResult, RateLimitInfo


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_deadline_remaining_and_cap():
    c = FakeClock()
    d = Deadline(10, clock=c)
    c.t += 4
    assert d.remaining() == pytest.approx(6) and d.cap(30) == pytest.approx(6) and d.cap(2) == 2
    c.t += 7
    assert d.expired() and d.remaining() == 0


def test_limiter_paces_by_tokens_and_settles_to_actual():
    c = FakeClock()
    L = ClientRateLimiter(LimiterConfig(rpm=30, tpm=8000, rpd=1000, tpd=10**6), c)
    for _ in range(4):
        assert L.wait_needed(1800) == 0
        L.reserve(1800)
    assert L.wait_needed(1800) == pytest.approx((1800 - 800) / (8000 / 60))
    L.settle(1800, 1000)  # the call used less than estimated: budget comes back
    assert L.tpm.available() == pytest.approx(1600)
    assert L.day_requests == 4 and L.day_tokens == 4 * 1800 - 800


def test_limiter_learns_daily_block_cooldown_and_changed_tpm():
    c = FakeClock()
    L = ClientRateLimiter(LimiterConfig(rpm=30, tpm=8000, rpd=1000, tpd=10**6), c)
    halved = CallResult("stand-in", "m", 0, ok=True, rate_limit=RateLimitInfo(limit_tokens_minute=4000))
    L.observe(halved)
    assert L.tpm.capacity == 4000 and L.observed_tpm_limit == 4000
    rpm429 = CallResult("stand-in", "m", 0, error_kind=RATE_LIMITED, limit_kind="RPM",
                        rate_limit=RateLimitInfo(retry_after_s=3))
    L.observe(rpm429)
    assert L.wait_needed(10) >= 3 - 1e-9
    rpd429 = CallResult("stand-in", "m", 0, error_kind=RATE_LIMITED, limit_kind="RPD",
                        rate_limit=RateLimitInfo(reset_requests_day_s=7200))
    L.observe(rpd429)
    with pytest.raises(DailyBudgetExhausted) as ei:
        L.daily_check(10)
    assert ei.value.kind == "RPD" and ei.value.reset_s == 7200


def test_limiter_daily_budget_from_own_count():
    L = ClientRateLimiter(LimiterConfig(rpm=30, tpm=8000, rpd=3, tpd=10**6, day_used_requests=3), FakeClock())
    with pytest.raises(DailyBudgetExhausted):
        L.daily_check(10)


def _policy(**kw):
    return RetryPolicy(rng=random.Random(1), **kw)


def test_retry_rules():
    p = _policy()
    assert not p.decide(CLIENT, None, None, 0, 30, 1).retry
    assert not p.decide(RATE_LIMITED, "RPD", 5, 0, 30, 1).retry
    d = p.decide(RATE_LIMITED, "TPM", 7.66, 0, 30, 1)
    assert d.retry and 7.66 <= d.wait_s <= 7.91          # honours retry-after
    assert not p.decide(RATE_LIMITED, "TPM", 7.66, 0, 8, 1).retry  # would miss the deadline
    d = p.decide(SERVER, None, None, 1, 30, 1)
    assert d.retry and 0 <= d.wait_s <= 1.0               # full jitter, base 0.5 * 2^1
    assert not p.decide(SERVER, None, None, 2, 30, 1).retry  # at most 2 retries


def test_retry_budget_stops_retry_storms():
    p = _policy(budget_cap=2, budget_ratio=0.0)
    assert p.decide(SERVER, None, None, 0, 30, 1).retry
    assert p.decide(SERVER, None, None, 0, 30, 1).retry
    d = p.decide(SERVER, None, None, 0, 30, 1)
    assert not d.retry and d.reason == "retry budget exhausted" and p.retries_denied_budget == 1


def test_naive_retry_ignores_waits():
    d = _policy(naive=True).decide(RATE_LIMITED, "TPM", 7.0, 0, 1, 1)
    assert d.retry and d.wait_s == 0


def _gate(max_in_flight=2, queue_max=3, tpm=10**6):
    L = ClientRateLimiter(LimiterConfig(rpm=10**6, tpm=tpm, rpd=10**6, tpd=10**9))
    return ModelGate(L, max_in_flight, queue_max, expected_service_s=0.05)


async def test_gate_caps_concurrency_and_rejects_when_queue_full():
    gate = _gate()
    held = [await gate.acquire(10, Deadline(5)), await gate.acquire(10, Deadline(5))]
    waiters = [asyncio.create_task(gate.acquire(10, Deadline(5))) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert gate.in_flight == 2 and gate.waiting == 3
    with pytest.raises(AdmissionRejected) as ei:
        await gate.acquire(10, Deadline(5))
    assert ei.value.reason == "queue_full"
    for p in held:
        p.release(10)
    got = [await w for w in waiters[:2]]
    assert gate.peak_in_flight == 2
    for p in got:
        p.release(10)
    (await waiters[2]).release(10)
    assert gate.in_flight == 0 and gate.waiting == 0


async def test_gate_rejects_at_arrival_when_wait_exceeds_deadline():
    gate = _gate(tpm=600)  # 10 tokens/s
    (await gate.acquire(600, Deadline(5))).release(600)
    with pytest.raises(AdmissionRejected) as ei:
        await gate.acquire(300, Deadline(5))   # needs ~30 s of refill
    assert ei.value.reason == "wait_exceeds_deadline" and ei.value.estimated_wait_s > 5
    assert gate.waiting == 0


async def test_gate_cancellation_leaves_no_leak():
    gate = _gate(max_in_flight=1)
    p = await gate.acquire(10, Deadline(5))
    t = asyncio.create_task(gate.acquire(10, Deadline(5)))
    await asyncio.sleep(0.02)
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    p.release(10)
    assert gate.waiting == 0 and gate.in_flight == 0 and gate.queued_tokens == 0


def test_results_writer_exact_columns_and_blank_first_token_on_failure(tmp_path):
    w = ResultsWriter(tmp_path / "results.csv", tmp_path / "traces.jsonl")
    ok = RequestTrace("r1", "steady", "stand-in", "medium_k4_rr")
    ok.mark_first_token()
    ok.finish("ok", "answered")
    bad = RequestTrace("r2", "burst", "stand-in", "medium_k4_rr")
    bad.mark_first_token()
    bad.finish("rate_limited", "queue_full")
    w.write(ok)
    w.write(bad)
    with (tmp_path / "results.csv").open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    assert rows[0] == RESULTS_COLUMNS and len(rows) == 3
    assert rows[1][RESULTS_COLUMNS.index("first_token_ms")] != ""
    assert rows[2][RESULTS_COLUMNS.index("first_token_ms")] == ""
    assert len((tmp_path / "traces.jsonl").read_text(encoding="utf-8").splitlines()) == 2
