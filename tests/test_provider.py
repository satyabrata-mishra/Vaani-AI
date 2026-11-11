import asyncio
import json

import httpx
import pytest

from app.config import load_settings
from app.providers.base import (
    CLIENT, RATE_LIMITED, SERVER, TIMEOUT_AFTER_FIRST_TOKEN, TIMEOUT_BEFORE_FIRST_TOKEN,
    ProviderError, limit_kind_from_message, parse_duration_s,
)
from app.providers.openai_compat import OpenAICompatProvider

HEADERS = {
    "x-ratelimit-limit-requests": "1000", "x-ratelimit-remaining-requests": "998",
    "x-ratelimit-reset-requests": "2m59.56s", "x-ratelimit-limit-tokens": "8000",
    "x-ratelimit-remaining-tokens": "7400", "x-ratelimit-reset-tokens": "7.66s",
}


def sse(*events) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


def delta(**d):
    return {"choices": [{"index": 0, "delta": d, "finish_reason": None}]}


def make(handler, tmp_path):
    return OpenAICompatProvider("http://test/openai/v1", "k", "openai/gpt-oss-20b", "stand-in",
                                load_settings(), transport=httpx.MockTransport(handler),
                                ledger_path=tmp_path / "ledger.jsonl")


async def collect(call):
    return [t async for t in call]


@pytest.mark.parametrize("value,expected", [
    ("7.66s", 7.66), ("2m59.56s", 179.56), ("1h2m3s", 3723.0), ("120ms", 0.12), ("2", 2.0),
    (None, None), ("garbage", None),
])
def test_parse_duration(value, expected):
    got = parse_duration_s(value)
    assert got == pytest.approx(expected) if expected is not None else got is None


def test_limit_kind_from_groq_message():
    msg = ("Rate limit reached for model `openai/gpt-oss-20b` in organization `org_x` "
           "on tokens per minute (TPM): Limit 8000, Used 7900, Requested 500.")
    assert limit_kind_from_message(msg) == "TPM"
    assert limit_kind_from_message("on requests per day (RPD): Limit 1000") == "RPD"
    assert limit_kind_from_message("something else") is None


async def test_stream_yields_content_not_reasoning_and_records_usage(tmp_path):
    seen_payload = {}

    def handler(request):
        seen_payload.update(json.loads(request.content))
        body = sse(delta(role="assistant"), delta(reasoning="thinking..."),
                   delta(content="Hello"), delta(content=" world"),
                   {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "x_groq": {"usage": {"prompt_tokens": 12, "completion_tokens": 7,
                                         "completion_tokens_details": {"reasoning_tokens": 3}}}})
        return httpx.Response(200, headers=HEADERS, content=body)

    p = make(handler, tmp_path)
    call = p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=50, timeout_s=5)
    assert await collect(call) == ["Hello", " world"]
    r = call.result
    assert r.ok and r.content == "Hello world" and r.finish_reason == "stop"
    assert (r.tokens_in, r.tokens_out, r.reasoning_tokens) == (12, 7, 3)
    assert r.t_first_reasoning <= r.t_first_token <= r.t_end
    assert r.rate_limit.remaining_tokens_minute == 7400
    assert r.rate_limit.reset_requests_day_s == pytest.approx(179.56)
    assert seen_payload["reasoning_effort"] == "low" and seen_payload["stream"] is True
    assert "Authorization" not in json.dumps(r.to_log())
    await p.aclose()


async def test_429_classified_with_retry_after_and_limit_kind(tmp_path):
    def handler(request):
        return httpx.Response(429, headers={**HEADERS, "retry-after": "3"}, json={"error": {
            "message": "Rate limit reached ... on requests per minute (RPM): Limit 30, Used 30",
            "type": "requests", "code": "rate_limit_exceeded"}})

    p = make(handler, tmp_path)
    call = p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=5, timeout_s=5)
    with pytest.raises(ProviderError) as ei:
        await collect(call)
    r = ei.value.result
    assert r is call.result
    assert (r.error_kind, r.status_code, r.limit_kind) == (RATE_LIMITED, 429, "RPM")
    assert r.rate_limit.retry_after_s == 3.0
    await p.aclose()


@pytest.mark.parametrize("status,kind", [(500, SERVER), (503, SERVER), (400, CLIENT), (413, CLIENT)])
async def test_http_error_kinds(tmp_path, status, kind):
    p = make(lambda req: httpx.Response(status, json={"error": {"message": "x"}}), tmp_path)
    with pytest.raises(ProviderError) as ei:
        await collect(p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=5, timeout_s=5))
    assert ei.value.kind == kind
    await p.aclose()


async def test_timeout_before_first_token(tmp_path):
    async def slow_body():
        await asyncio.sleep(1.0)
        yield sse(delta(content="late"))

    p = make(lambda req: httpx.Response(200, content=slow_body()), tmp_path)
    call = p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=5, timeout_s=5,
                         first_token_timeout_s=0.1)
    with pytest.raises(ProviderError) as ei:
        await collect(call)
    assert ei.value.kind == TIMEOUT_BEFORE_FIRST_TOKEN
    assert call.result.total_ms < 900
    await p.aclose()


async def test_timeout_after_first_token_keeps_partial_content(tmp_path):
    async def stalls():
        yield f"data: {json.dumps(delta(content='partial'))}\n\n".encode()
        await asyncio.sleep(1.0)
        yield b"data: [DONE]\n\n"

    p = make(lambda req: httpx.Response(200, content=stalls()), tmp_path)
    call = p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=5, timeout_s=0.2)
    got = []
    with pytest.raises(ProviderError) as ei:
        async for t in call:
            got.append(t)
    assert got == ["partial"]
    assert ei.value.kind == TIMEOUT_AFTER_FIRST_TOKEN
    await p.aclose()


async def test_error_event_inside_stream(tmp_path):
    body = sse(delta(content="a"), {"error": {"message": "internal"}})
    p = make(lambda req: httpx.Response(200, content=body), tmp_path)
    with pytest.raises(ProviderError) as ei:
        await collect(p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=5, timeout_s=5))
    assert ei.value.kind == SERVER
    await p.aclose()


async def test_non_streaming_complete(tmp_path):
    def handler(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(200, headers=HEADERS, json={
            "choices": [{"message": {"content": "ok", "reasoning": "r"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2}})

    p = make(handler, tmp_path)
    r = await p.complete([{"role": "user", "content": "hi"}], max_tokens=5, timeout_s=5)
    assert r.ok and r.content == "ok" and r.tokens_in == 5 and r.first_token_ms is not None
    await p.aclose()


async def test_ledger_written_per_attempt(tmp_path):
    p = make(lambda req: httpx.Response(429, json={"error": {"message": "(RPM)"}}), tmp_path)
    for _ in range(2):
        with pytest.raises(ProviderError):
            await collect(p.stream_chat([{"role": "user", "content": "hi"}], max_tokens=5, timeout_s=5))
    lines = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["status_code"] == 429
    await p.aclose()


def test_real_provider_refuses_without_key():
    with pytest.raises(RuntimeError):
        OpenAICompatProvider.groq(load_settings(groq_api_key=""))
