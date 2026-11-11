"""HTTP entry point. POST /ask streams server-sent events: {"type":"token"} chunks of the verified
answer, then one {"type":"done"} event with status and sources. If the client disconnects, the
request is cancelled and recorded as status=timeout (outcome client_gave_up).

Run:  BACKEND=standin uvicorn app.main:app --port 8000      (stand-in must be running)
      BACKEND=real    uvicorn app.main:app --port 8000      (uses GROQ_API_KEY from .env)"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import DEFAULT_RETRIEVAL, RETRIEVAL_SETTINGS, load_settings
from app.infra.metrics import ResultsWriter
from app.pipeline.orchestrator import Pipeline
from app.providers.openai_compat import OpenAICompatProvider


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    retrieval_setting: str = DEFAULT_RETRIEVAL
    deadline_s: float | None = Field(default=None, gt=0, le=120)


def build_pipeline(backend: str | None = None, **settings_overrides) -> Pipeline:
    settings = load_settings(**settings_overrides)
    backend = (backend or os.getenv("BACKEND", "standin")).lower()
    provider = OpenAICompatProvider.groq(settings) if backend == "real" else OpenAICompatProvider.standin(settings)
    return Pipeline(settings, provider, writer=ResultsWriter())


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pipeline = build_pipeline()
    yield
    await app.state.pipeline.provider.aclose()


app = FastAPI(title="The Relay", lifespan=lifespan)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


@app.post("/ask")
async def ask(req: AskRequest):
    if req.retrieval_setting not in RETRIEVAL_SETTINGS:
        raise HTTPException(400, f"unknown retrieval_setting; choose one of {list(RETRIEVAL_SETTINGS)}")
    pipeline: Pipeline = app.state.pipeline
    out: asyncio.Queue = asyncio.Queue(maxsize=1000)

    async def sink(text: str):
        await out.put(_sse({"type": "token", "text": text}))

    async def work():
        try:
            trace = await pipeline.handle(req.question, scenario="interactive",
                                          retrieval_setting=req.retrieval_setting,
                                          deadline_s=req.deadline_s, on_token=sink)
            await out.put(_sse({"type": "done", "request_id": trace.request_id, "status": trace.status,
                                "outcome": trace.outcome, "sources": trace.citations,
                                "first_token_ms": trace.first_token_ms, "total_ms": trace.total_ms}))
        finally:
            await out.put(None)

    async def stream():
        task = asyncio.create_task(work())
        try:
            while (item := await out.get()) is not None:
                yield item
        finally:
            if not task.done():
                task.cancel()   # client went away: stop the work and record it as given up

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.get("/health")
async def health():
    p: Pipeline = app.state.pipeline
    return {"source": p.provider.source, "queue_waiting": p.gate.waiting, "in_flight": p.gate.in_flight,
            "tpm_headroom": round(p.limiter.tpm_headroom(), 3), "daily_blocked": p.limiter.daily_blocked}
