import asyncio
import random
import socket
import threading
import time
from dataclasses import replace

import httpx
import pytest
import uvicorn

from app.config import load_settings
from app.providers.base import (RATE_LIMITED, SERVER, TIMEOUT_BEFORE_FIRST_TOKEN, ProviderError,
                                limit_kind_from_message, parse_duration_s)
from app.providers.openai_compat import OpenAICompatProvider
from app.simulator.behavior import (Faults, Limits, RateLimiter, TokenBucket, extractive_answer,
                                    format_duration)
from app.simulator.profiles import build_profiles, calibrated_normal
from app.simulator.server import create_app


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# --- unit: limits and formats --------------------------------------------------------------

def test_bucket_refills_continuously_and_allows_debt():
    c = FakeClock()
    b = TokenBucket(8000, 8000 / 60, c)
    b.charge(8094)
    assert b.available() == pytest.approx(-94)
    c.t += 1.41
    assert b.available() == pytest.approx(-94 + 1.41 * 8000 / 60)
    assert b.seconds_until(100) == pytest.approx((100 - b.available()) / (8000 / 60))


def test_rpm_burst_then_reject_and_rejections_cost_nothing():
    c = FakeClock()
    rl = RateLimiter(Limits(rpm=30, tpm=10**9, rpd=1000, tpd=10**9), c)
    assert all(rl.admit(10) is None for _ in range(30))
    rej = rl.admit(10)
    assert rej.limit == "RPM" and rej.retry_after_s == pytest.approx(2.0)
    assert limit_kind_from_message(rej.message("m")) == "RPM"
    assert rl.day_requests == 30  # the rejected request was not counted
    c.t += 2.0
    assert rl.admit(10) is None


def test_tpm_checks_prompt_and_charges_output_afterwards():
    c = FakeClock()
    rl = RateLimiter(Limits(rpm=1000, tpm=8000, rpd=1000, tpd=10**9), c)
    for _ in range(4):
        assert rl.admit(1676) is None
        rl.charge_output(50)
    rej = rl.admit(1692)
    assert rej.limit == "TPM"
    assert rej.retry_after_s == pytest.approx((1692 - (8000 - 4 * 1726)) / (8000 / 60))
    assert "tokens per minute (TPM): Limit 8000" in rej.message("m")


def test_daily_limits_and_groq_header_semantics():
    rl = RateLimiter(Limits(rpm=10**6, tpm=10**9, rpd=1000, tpd=10**9), FakeClock(), day_used_requests=998)
    assert rl.admit(10) is None
    h = rl.headers()
    assert h["x-ratelimit-remaining-requests"] == "1"
    assert parse_duration_s(h["x-ratelimit-reset-requests"]) == pytest.approx(999 * 86.4, abs=0.01)
    assert rl.admit(10) is None
    assert rl.admit(10).limit == "RPD"


@pytest.mark.parametrize("seconds,text", [(0.705, "705ms"), (7.66, "7.66s"), (179.56, "2m59.56s"),
                                          (6566.4, "1h49m26.4s"), (2.0, "2s")])
def test_format_duration_matches_groq_and_round_trips(seconds, text):
    assert format_duration(seconds) == text
    assert parse_duration_s(text) == pytest.approx(seconds)


def test_extractive_answer_cites_passages_or_refuses():
    msgs = [{"role": "user", "content": "Passages:\n[P1] Attention, §3.2.1, p.4\nWe scale the dot products by "
                                        "1/sqrt(dk) to counteract small gradients. More.\n[P2] x\nShort.\n\nQuestion: why?"}]
    ans = extractive_answer(msgs, 50)
    assert "[P1]" in ans and "scale the dot products" in ans
    assert extractive_answer([{"role": "user", "content": "no passages"}], 50) == "NOT_IN_PAPERS"


def test_latency_model_calibrated_and_slower_under_load():
    lat = calibrated_normal().latency
    rng = random.Random(1)
    idle = sorted(lat.first_byte_s(rng, 1500, 1)[0] for _ in range(2000))
    busy = sorted(lat.first_byte_s(rng, 1500, 9)[0] for _ in range(2000))
    median_idle = idle[1000] * 1000
    assert abs(median_idle - (lat.base_ms + lat.per_1k_input_ms * 1.5)) < 60
    assert busy[1000] > idle[1000] * 1.2


def test_profiles_exist_and_differ_from_normal():
    p = build_profiles()
    assert {"normal", "high_latency", "flaky", "rate_limited", "daily_exhausted",
            "heavy_concurrency", "overload"} <= set(p)
    assert p["rate_limited"].limits.tpm == p["normal"].limits.tpm // 2
    assert p["daily_exhausted"].day_used_requests > 0


# --- integration: real server over HTTP ------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def standin():
    servers = []

    def start(profile="normal", scale=0.05, **changes):
        profiles = build_profiles()
        if changes:
            profiles[profile] = replace(profiles[profile], **changes)
        app = create_app(profile, seed=0, scale=scale, profiles=profiles)
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.02)
        servers.append((server, th))
        settings = load_settings(standin_base_url=f"http://127.0.0.1:{port}/openai/v1")
        return OpenAICompatProvider.standin(settings), app, f"http://127.0.0.1:{port}"

    yield start
    for server, th in servers:
        server.should_exit = True
        th.join(timeout=5)


PROMPT = [{"role": "system", "content": "Answer only from passages."},
          {"role": "user", "content": "Passages:\n[P1] Attention, §3.2.1, p.4\nWe scale the dot products by "
                                      "one over the square root of dk to counteract small gradients.\n\nQuestion: why scale?"}]


async def consume(call):
    return "".join([t async for t in call])


async def test_stream_ok_with_usage_headers_and_timings(standin):
    p, app, _ = standin()
    call = p.stream_chat(PROMPT, max_tokens=300, timeout_s=10)
    text = await consume(call)
    r = call.result
    assert r.ok and "[P1]" in text
    assert r.tokens_in > 0 and r.tokens_out > 0 and r.reasoning_tokens == 6
    assert r.t_headers <= r.t_first_reasoning <= r.t_first_token <= r.t_end
    assert r.rate_limit.limit_tokens_minute == 8000 and r.rate_limit.remaining_requests_day == 999
    assert r.source == "stand-in"
    await p.aclose()


async def test_rate_limited_profile_returns_groq_style_429(standin):
    p, app, _ = standin(limits=Limits(rpm=2, tpm=10**6, rpd=1000, tpd=10**9))
    for _ in range(2):
        await consume(p.stream_chat(PROMPT, max_tokens=50, timeout_s=10))
    with pytest.raises(ProviderError) as ei:
        await consume(p.stream_chat(PROMPT, max_tokens=50, timeout_s=10))
    r = ei.value.result
    assert (r.error_kind, r.limit_kind) == (RATE_LIMITED, "RPM")
    assert r.rate_limit.retry_after_s >= 1
    await p.aclose()


async def test_daily_exhausted_profile(standin):
    p, app, _ = standin("daily_exhausted", day_used_requests=999)
    await consume(p.stream_chat(PROMPT, max_tokens=50, timeout_s=10))
    with pytest.raises(ProviderError) as ei:
        await consume(p.stream_chat(PROMPT, max_tokens=50, timeout_s=10))
    assert ei.value.result.limit_kind == "RPD"
    await p.aclose()


async def test_server_error_fault(standin):
    p, app, _ = standin(faults=Faults(error_rate=1.0))
    with pytest.raises(ProviderError) as ei:
        await consume(p.stream_chat(PROMPT, max_tokens=50, timeout_s=10))
    assert ei.value.kind == SERVER and ei.value.result.status_code == 500
    await p.aclose()


async def test_midstream_error_after_first_token(standin):
    p, app, _ = standin(faults=Faults(midstream_error_rate=1.0), answer_tokens=60)
    call = p.stream_chat(PROMPT, max_tokens=300, timeout_s=10)
    with pytest.raises(ProviderError) as ei:
        await consume(call)
    assert ei.value.kind == SERVER and call.result.t_first_token is not None
    await p.aclose()


async def test_hang_makes_client_time_out_and_capacity_is_released(standin):
    p, app, base = standin(faults=Faults(hang_rate=1.0, hang_s=2.0), scale=1.0)
    with pytest.raises(ProviderError) as ei:
        await consume(p.stream_chat(PROMPT, max_tokens=50, timeout_s=0.3))
    assert ei.value.kind == TIMEOUT_BEFORE_FIRST_TOKEN
    await asyncio.sleep(2.3)
    async with httpx.AsyncClient() as c:
        assert (await c.get(f"{base}/admin/state")).json()["in_flight"] == 0
    await p.aclose()


async def test_client_disconnect_mid_stream_releases_capacity(standin):
    slow_gen = replace(calibrated_normal().latency, tokens_per_s=20)  # ~25-token answer takes > 1 s
    p, app, base = standin(scale=1.0, latency=slow_gen)
    call = p.stream_chat(PROMPT, max_tokens=500, timeout_s=10)
    agen = call.__aiter__()
    await agen.__anext__()
    await agen.aclose()
    await asyncio.sleep(0.5)
    async with httpx.AsyncClient() as c:
        st = (await c.get(f"{base}/admin/state")).json()
        log = (await c.get(f"{base}/admin/log")).json()
    assert st["in_flight"] == 0
    assert log[-1]["outcome"] == "client_disconnected"
    assert log[-1]["out_tokens"] < 6 + 25  # only what was generated before the disconnect is charged
    await p.aclose()


async def test_admin_switches_profile(standin):
    p, app, base = standin()
    async with httpx.AsyncClient() as c:
        st = (await c.post(f"{base}/admin/profile", json={"name": "rate_limited"})).json()
        assert st["profile"] == "rate_limited" and st["limits"]["tpm"] == 4000
        assert (await c.post(f"{base}/admin/profile", json={"name": "nope"})).status_code == 400
    await p.aclose()


async def test_non_streaming(standin):
    p, app, _ = standin()
    r = await p.complete(PROMPT, max_tokens=300, timeout_s=10)
    assert r.ok and "[P1]" in r.content and r.tokens_out > 0
    await p.aclose()
