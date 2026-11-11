"""The stand-in model service: Groq's OpenAI-compatible chat endpoint, with controllable behaviour.

    python -m app.simulator.server --profile normal --port 8100 [--seed 0] [--scale 1.0]

Admin endpoints (used by experiment scripts between scenarios):
    GET  /admin/state            profile, counters, in-flight
    POST /admin/profile          {"name": "rate_limited", "seed": 1, "scale": 1.0}
    POST /admin/reset            fresh limiter + counters, same profile
    GET  /admin/log              every request the stand-in handled
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import time
import uuid
from dataclasses import asdict, replace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.ingest.chunker import approx_tokens
from app.simulator.behavior import (RateLimiter, Stats, count_prompt_tokens, extractive_answer)
from app.simulator.profiles import Profile, build_profiles


class StandIn:
    def __init__(self, profile: Profile, seed: int = 0, scale: float = 1.0):
        self.configure(profile, seed, scale)

    def configure(self, profile: Profile, seed: int, scale: float):
        self.profile = replace(profile, latency=replace(profile.latency, scale=scale))
        self.seed = seed
        self.rng = random.Random(seed)
        self.limiter = RateLimiter(profile.limits, day_used_requests=profile.day_used_requests,
                                   day_used_tokens=profile.day_used_tokens)
        self.capacity = asyncio.Semaphore(profile.capacity)
        self.stats = Stats()
        self.log: list[dict] = []
        self.t0 = time.monotonic()

    def state(self) -> dict:
        p = self.profile
        return {"profile": p.name, "description": p.description, "seed": self.seed,
                "scale": p.latency.scale, "limits": asdict(p.limits), "capacity": p.capacity,
                "faults": asdict(p.faults), "in_flight": self.stats.in_flight,
                "peak_in_flight": self.stats.peak_in_flight, "outcomes": self.stats.by_outcome,
                "day_requests": self.limiter.day_requests, "day_tokens": self.limiter.day_tokens,
                "headers": self.limiter.headers()}


def _chunk(content: str, tokens_per_chunk: int = 4) -> list[str]:
    words = content.split(" ")
    out, cur = [], []
    for w in words:
        cur.append(w)
        if approx_tokens(" ".join(cur)) >= tokens_per_chunk:
            out.append(" ".join(cur) + " ")
            cur = []
    if cur:
        out.append(" ".join(cur))
    return out


def create_app(profile_name: str = "normal", seed: int = 0, scale: float = 1.0,
               profiles: dict[str, Profile] | None = None) -> FastAPI:
    profiles = profiles or build_profiles()
    app = FastAPI(title="The Relay stand-in model service")
    sim = StandIn(profiles[profile_name], seed, scale)
    app.state.sim = sim

    def record(**kw):
        sim.stats.count(kw["outcome"])
        sim.log.append({"t": round(time.monotonic() - sim.t0, 4), **kw})

    @app.get("/openai/v1/models")
    async def models():
        return {"object": "list", "data": [{"id": "openai/gpt-oss-20b", "object": "model"}]}

    @app.post("/openai/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        p, lat = sim.profile, sim.profile.latency
        model = body.get("model", "openai/gpt-oss-20b")
        messages = body.get("messages", [])
        stream = bool(body.get("stream"))
        max_tokens = int(body.get("max_completion_tokens") or body.get("max_tokens") or 1024)
        prompt_tokens = count_prompt_tokens(messages)
        req_id = f"req_standin_{uuid.uuid4().hex[:16]}"

        rej = sim.limiter.admit(prompt_tokens)
        if rej:
            await asyncio.sleep(p.rejection_ms * lat.scale / 1000)
            record(outcome=f"429_{rej.limit}", prompt_tokens=prompt_tokens, in_flight=sim.stats.in_flight)
            headers = {**sim.limiter.headers(), "retry-after": str(max(1, math.ceil(rej.retry_after_s))),
                       "x-request-id": req_id}
            return JSONResponse(status_code=429, headers=headers, content={"error": {
                "message": rej.message(model), "type": "tokens" if rej.limit in ("TPM", "TPD") else "requests",
                "code": "rate_limit_exceeded"}})

        stats, sem = sim.stats, sim.capacity  # a profile switch mid-request must not cross these
        stats.in_flight += 1
        stats.started += 1
        stats.peak_in_flight = max(stats.peak_in_flight, stats.in_flight)
        released = False

        def release():
            nonlocal released
            if not released:
                released = True
                stats.in_flight -= 1
                sem.release()

        t_arrive = time.monotonic()
        try:
            await sem.acquire()
        except BaseException:
            stats.in_flight -= 1
            raise
        try:
            capacity_wait_ms = (time.monotonic() - t_arrive) * 1000
            in_flight = stats.in_flight
            first_byte_s, slow = lat.first_byte_s(sim.rng, prompt_tokens, in_flight)
            roll = sim.rng.random()
            f = p.faults
            base_log = dict(prompt_tokens=prompt_tokens, in_flight=in_flight, slow_start=slow,
                            capacity_wait_ms=round(capacity_wait_ms, 1),
                            first_byte_ms=round(first_byte_s * 1000, 1))
            if roll < f.hang_rate:
                record(outcome="hang", **base_log)
                await asyncio.sleep(f.hang_s * lat.scale)
                release()
                return JSONResponse(status_code=504, content={"error": {"message": "Gateway Timeout"}})
            await asyncio.sleep(first_byte_s)
            if roll < f.hang_rate + f.error_rate:
                record(outcome="500", **base_log)
                release()
                return JSONResponse(status_code=500, headers=sim.limiter.headers(), content={"error": {
                    "message": "Internal Server Error", "type": "internal_server_error"}})

            reasoning = p.reasoning_tokens
            content = extractive_answer(messages, min(p.answer_tokens, max(1, max_tokens - reasoning)))
            content_tokens = approx_tokens(content)
            finish = "stop"
            if reasoning + content_tokens > max_tokens:
                keep_chars = max(0, int((max_tokens - reasoning) * 4.5))
                content, content_tokens, finish = content[:keep_chars], max(0, max_tokens - reasoning), "length"
            midstream_fail = sim.rng.random() < f.midstream_error_rate
            headers = {**sim.limiter.headers(), "x-request-id": req_id,
                       "x-standin-in-flight": str(in_flight), "x-standin-slow-start": str(int(slow)),
                       "x-standin-capacity-wait-ms": f"{capacity_wait_ms:.1f}"}
            created = int(time.time())

            def usage(out_tokens):
                return {"prompt_tokens": prompt_tokens, "completion_tokens": out_tokens,
                        "total_tokens": prompt_tokens + out_tokens,
                        "completion_tokens_details": {"reasoning_tokens": reasoning}}

            if not stream:
                gen_s = lat.header_to_token_s(prompt_tokens, in_flight) + (reasoning + content_tokens) * lat.per_token_s(in_flight)
                await asyncio.sleep(gen_s)
                out = reasoning + content_tokens
                sim.limiter.charge_output(out)
                record(outcome="ok", stream=False, out_tokens=out, **base_log)
                release()
                return JSONResponse(headers=headers, content={
                    "id": req_id, "object": "chat.completion", "created": created, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": content,
                                                         "reasoning": "(stand-in reasoning)"},
                                 "finish_reason": finish}],
                    "usage": usage(out)})

            async def events():
                emitted = 0
                outcome = "ok"
                try:
                    def ev(delta, finish_reason=None, extra=None):
                        d = {"id": req_id, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
                        if extra:
                            d.update(extra)
                        return f"data: {json.dumps(d)}\n\n"

                    yield ev({"role": "assistant"})
                    await asyncio.sleep(lat.header_to_token_s(prompt_tokens, in_flight))
                    yield ev({"reasoning": "(stand-in reasoning) "})
                    emitted += reasoning
                    await asyncio.sleep(reasoning * lat.per_token_s(in_flight))
                    chunks = _chunk(content) if content else []
                    for i, piece in enumerate(chunks):
                        if midstream_fail and i == len(chunks) // 2:
                            outcome = "midstream_error"
                            yield f"data: {json.dumps({'error': {'message': 'stand-in: upstream error mid-stream', 'type': 'internal_server_error'}})}\n\n"
                            return
                        yield ev({"content": piece})
                        t = approx_tokens(piece)
                        emitted += t
                        await asyncio.sleep(t * lat.per_token_s(in_flight))
                    yield ev({}, finish, {"x_groq": {"id": req_id, "usage": usage(emitted)}})
                    yield "data: [DONE]\n\n"
                except asyncio.CancelledError:
                    outcome = "client_disconnected"
                    raise
                finally:
                    sim.limiter.charge_output(emitted)
                    record(outcome=outcome, stream=True, out_tokens=emitted, **base_log)
                    release()

            return StreamingResponse(events(), media_type="text/event-stream", headers=headers)
        except BaseException:
            release()
            raise

    @app.get("/admin/state")
    async def state():
        return sim.state()

    @app.post("/admin/profile")
    async def set_profile(cfg: dict):
        name = cfg.get("name", sim.profile.name)
        if name not in profiles:
            return JSONResponse(status_code=400, content={"error": f"unknown profile {name}",
                                                          "profiles": list(profiles)})
        sim.configure(profiles[name], int(cfg.get("seed", sim.seed)), float(cfg.get("scale", sim.profile.latency.scale)))
        return sim.state()

    @app.post("/admin/reset")
    async def reset():
        base = profiles[sim.profile.name]
        sim.configure(base, sim.seed, sim.profile.latency.scale)
        return sim.state()

    @app.get("/admin/log")
    async def get_log():
        return sim.log

    return app


def main():
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="normal")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--scale", type=float, default=1.0)
    args = ap.parse_args()
    uvicorn.run(create_app(args.profile, args.seed, args.scale), host="127.0.0.1", port=args.port,
                log_level="warning")


if __name__ == "__main__":
    main()
