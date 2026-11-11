"""Replay the real-Groq probe shapes against the stand-in and compare. No Groq quota used.

    python -m experiments.standin_check [--per-size 40]

Starts the stand-in in-process on a free port, writes results/standin_check.json."""
from __future__ import annotations

import argparse
import asyncio
import json
import socket
import threading
import time

import httpx
import uvicorn

from app.config import RESULTS_DIR, load_settings
from app.evaluation.stats import percentile
from app.providers.base import ProviderError
from app.providers.openai_compat import OpenAICompatProvider
from app.simulator.server import create_app
from experiments.real_probe import TINY, passage_messages

SIZES = [300, 1500, 3000]


def start_server(profile: str):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(profile, seed=42), host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    return server, f"http://127.0.0.1:{port}"


async def one(p, messages, max_tokens):
    call = p.stream_chat(messages, max_tokens=max_tokens, timeout_s=60)
    try:
        async for _ in call:
            pass
    except ProviderError:
        pass
    return call.result


def ms(r, k):
    return None if getattr(r, k) is None else (getattr(r, k) - r.t_start) * 1000


def dist(v):
    v = [x for x in v if x is not None]
    return {"n": len(v), "p50": percentile(v, 50), "p95": percentile(v, 95), "max": max(v) if v else None}


def real_latency():
    s = json.loads((RESULTS_DIR / "real_probe" / "summary.json").read_text())["latency"]
    return {int(k): v for k, v in s.items()}


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-size", type=int, default=40)
    args = ap.parse_args()
    out = {}

    # 1. latency shape, limits disabled (the real probe was paced to stay under TPM)
    server, base = start_server("unlimited")
    p = OpenAICompatProvider.standin(load_settings(standin_base_url=f"{base}/openai/v1"))
    rows = {s: [] for s in SIZES}
    msgs = {s: passage_messages(s) for s in SIZES}
    for _ in range(args.per_size):
        for s in SIZES:
            rows[s].append(await one(p, msgs[s], 300))
    real = real_latency()
    out["latency"] = {}
    print(f"{'size':<6}{'source':<10}{'n':>4} {'first byte p50/p95/max':>26} {'first token p50/p95/max':>26}")
    for s in SIZES:
        sim = {"first_byte": dist(ms(r, "t_headers") for r in rows[s]),
               "first_token": dist(ms(r, "t_first_token") for r in rows[s]),
               "total": dist(ms(r, "t_end") for r in rows[s]),
               "tokens_in": dist(r.tokens_in for r in rows[s])}
        out["latency"][s] = sim
        rl = real[s]
        print(f"{s:<6}{'real':<10}{rl['headers_ms']['n']:>4} "
              f"{rl['headers_ms']['p50']:>8.0f}/{rl['headers_ms']['p95']:>6.0f}/{rl['headers_ms']['max']:>6.0f}      "
              f"{rl['first_token_ms']['p50']:>8.0f}/{rl['first_token_ms']['p95']:>6.0f}/{rl['first_token_ms']['max']:>6.0f}"
              f"   tokens_in {rl['tokens_in']['p50']:.0f}")
        print(f"{'':<6}{'stand-in':<10}{sim['first_byte']['n']:>4} "
              f"{sim['first_byte']['p50']:>8.0f}/{sim['first_byte']['p95']:>6.0f}/{sim['first_byte']['max']:>6.0f}      "
              f"{sim['first_token']['p50']:>8.0f}/{sim['first_token']['p95']:>6.0f}/{sim['first_token']['max']:>6.0f}"
              f"   tokens_in {sim['tokens_in']['p50']:.0f}")
    await p.aclose()
    server.should_exit = True

    # 2. RPM burst replay (real: 30 accepted in two runs) and 3. TPM burst replay (real: 4 accepted)
    for name, msgs_, max_t, interval, cap in [("rpm", TINY, 16, 0.5, 40), ("tpm", passage_messages(2000), 50, 0.0, 8)]:
        server, base = start_server("normal")
        p = OpenAICompatProvider.standin(load_settings(standin_base_url=f"{base}/openai/v1"))
        accepted, first_429 = 0, None
        for _ in range(cap):
            t0 = time.monotonic()
            r = await one(p, msgs_, max_t)
            if r.status_code == 429:
                first_429 = {"message": r.error_message, "retry_after_s": r.rate_limit.retry_after_s,
                             "latency_ms": ms(r, "t_end")}
                break
            accepted += 1
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))
        out[name] = {"accepted_before_429": accepted, "first_429": first_429}
        print(f"{name} burst: stand-in accepted {accepted} before 429; {first_429 and first_429['message'][:110]}")
        await p.aclose()
        server.should_exit = True

    (RESULTS_DIR / "standin_check.json").write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
