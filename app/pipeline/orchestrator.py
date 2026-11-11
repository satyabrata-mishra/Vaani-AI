"""The six steps for one question: intake -> analyze -> retrieve (tool) -> decide (branch/loop)
-> generate + verify (bounded loop) -> deliver. Every request ends with exactly one status and
one results.csv row, including when the person gives up (cancellation)."""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

from app.config import DEFAULT_RETRIEVAL, DEGRADED_PIECES, RETRIEVAL_SETTINGS, Settings
from app.infra.admission import AdmissionRejected, ModelGate
from app.infra.deadline import Deadline, DeadlineExceeded
from app.infra.metrics import RequestTrace, ResultsWriter
from app.infra.rate_limiter import ClientRateLimiter, DailyBudgetExhausted, LimiterConfig
from app.infra.retry import RetryPolicy
from app.pipeline.analyzer import analyze
from app.pipeline.prompts import build_messages, regenerate_messages
from app.pipeline.retrieval import RetrievalResult, RetrievalTool, ToolError, ToolTimeout
from app.pipeline.verifier import render, verify
from app.providers.base import (RATE_LIMITED, TIMEOUT_AFTER_FIRST_TOKEN, TIMEOUT_BEFORE_FIRST_TOKEN,
                                ProviderError)
from app.providers.openai_compat import OpenAICompatProvider
from app.ingest.chunker import count_prompt_tokens

TokenSink = Callable[[str], Awaitable[None]]

MSG_NOT_IN_PAPERS = ("The three papers do not answer this question, so I won't guess. "
                     "Try asking about the Transformer (Attention Is All You Need), Moshi, or H-Net.")
MSG_UNVERIFIED = ("I could not produce an answer whose every statement is supported by the papers, "
                  "so I am not giving one.")


@dataclass
class Reply:
    text: str
    sources: list[str]


def limiter_config(settings: Settings, **overrides) -> LimiterConfig:
    cfg = dict(rpm=settings.rpm_limit, tpm=settings.tpm_limit, rpd=settings.rpd_limit,
               tpd=settings.tpd_limit, rpm_burst=22)  # burst 22 fitted to the real RPM probe (R2)
    cfg.update(overrides)
    return LimiterConfig(**cfg)


class Pipeline:
    def __init__(self, settings: Settings, provider: OpenAICompatProvider, *,
                 tool: RetrievalTool | None = None, limiter: ClientRateLimiter | None = None,
                 retry: RetryPolicy | None = None, writer: ResultsWriter | None = None,
                 retrieval_setting: str = DEFAULT_RETRIEVAL):
        self.s = settings
        self.provider = provider
        self.tool = tool or RetrievalTool(settings.tool_timeout_s, rerank_slots=settings.rerank_slots)
        self.limiter = limiter or ClientRateLimiter(limiter_config(settings))
        self.gate = ModelGate(self.limiter, settings.max_in_flight, settings.queue_max,
                              settings.expected_service_s)
        self.retry = retry or RetryPolicy(settings.max_retries, settings.backoff_base_s,
                                          settings.backoff_cap_s, settings.retry_budget_ratio)
        self.writer = writer
        self.retrieval_setting = retrieval_setting

    # ---------------------------------------------------------------------------------------
    async def handle(self, question: str, *, scenario: str = "adhoc", question_id: str | None = None,
                     retrieval_setting: str | None = None, deadline_s: float | None = None,
                     on_token: TokenSink | None = None, run_id: str | None = None) -> RequestTrace:
        setting = retrieval_setting or self.retrieval_setting
        trace = RequestTrace(request_id=uuid.uuid4().hex[:12], scenario=scenario, source=self.provider.source,
                             retrieval_setting=setting, question_id=question_id, run_id=run_id)
        deadline = Deadline(deadline_s or self.s.deadline_s)
        try:
            status, outcome = await self._run(question, setting, trace, deadline, on_token)
            trace.finish(status, outcome)
        except asyncio.CancelledError:
            trace.finish("timeout", "client_gave_up")
            self._write(trace)
            raise
        except Exception as e:  # anything unexpected is still one honest row
            trace.event("unexpected_error", error=f"{type(e).__name__}: {e}"[:300])
            trace.finish("error", "unexpected_error")
        self._write(trace)
        return trace

    def _write(self, trace: RequestTrace):
        if self.writer:
            self.writer.write(trace)

    async def _deliver(self, trace: RequestTrace, reply: Reply, on_token: TokenSink | None,
                       counts_as_answer: bool = True):
        if counts_as_answer:
            trace.mark_first_token()
        trace.answer, trace.citations = reply.text, reply.sources
        if on_token:
            for word in reply.text.split(" "):
                await on_token(word + " ")
            if reply.sources:
                await on_token("\n\nSources:\n" + "\n".join(reply.sources))

    # ---------------------------------------------------------------------------------------
    async def _run(self, question, setting, trace, deadline, on_token) -> tuple[str, str]:
        # 1-2. intake + analyze
        with trace.span("analyze"):
            try:
                a = analyze(question)
            except ValueError as e:
                await self._deliver(trace, Reply(str(e), []), on_token, counts_as_answer=False)
                return "error", "invalid_input"
        trace.event("papers_named", papers=a.papers)

        # decide (before retrieval): degraded mode when the model budget is under pressure
        degraded = (self.limiter.tpm_headroom() < self.s.degrade_tpm_headroom
                    or self.gate.waiting >= self.s.degrade_queue_len)
        trace.degraded = degraded
        # Degrading cuts prompt tokens only. Re-ranking costs CPU, not provider budget, and its score is
        # what refuses off-topic questions, so it stays on (the tool sheds it only when its own queue is full).
        pieces, rerank = (DEGRADED_PIECES, None) if degraded else (None, None)
        papers = tuple(a.papers) if a.multi_paper else None

        # 3. retrieve (tool, with one retry on failure/timeout)
        try:
            result = await self._retrieve(a.question, setting, trace, deadline, pieces=pieces,
                                          rerank=rerank, papers=papers)
        except ToolError as e:
            await self._deliver(trace, Reply("The paper search is unavailable right now; please try again.", []),
                                on_token, counts_as_answer=False)
            trace.event("tool_failed", error=str(e))
            return ("timeout" if deadline.expired() else "error"), "tool_failed"

        # 4. decide: refuse without the model / widen the search once
        top = result.top_rerank
        if not result.passages or (top is not None and top < self.s.refuse_below_rerank):
            trace.event("refuse_before_model", top_rerank=top)
            await self._deliver(trace, Reply(MSG_NOT_IN_PAPERS, []), on_token)
            return "ok", "refused_no_evidence"
        if (top is not None and top < self.s.widen_below_rerank and not degraded
                and deadline.remaining() > self.s.tool_timeout_s + 2 * self.s.expected_service_s):
            fetch = RETRIEVAL_SETTINGS[setting].fetch * 2
            try:
                wider = await self._retrieve(a.question, setting, trace, deadline, fetch=fetch, papers=papers,
                                             span="retrieve_wider")
                trace.event("widened", before=top, after=wider.top_rerank)
                if wider.top_rerank is not None and wider.top_rerank > top:
                    result = wider
            except ToolError:
                trace.event("widen_failed")

        passages = result.passages
        messages = build_messages(a.question, passages)

        # 5. generate + verify; one regeneration unless degraded
        rounds = 1 if degraded else 2
        try:
            for round_no in range(rounds):
                with trace.span("generate", round=round_no):
                    text = await self._generate(messages, trace, deadline)
                with trace.span("verify", round=round_no):
                    v = verify(text, passages, a.papers, self.s.min_overlap)
                trace.event("verified", round=round_no, refused=v.refused, kept=len(v.verified),
                            dropped=[s.reason for s in v.dropped])
                if v.refused:
                    await self._deliver(trace, Reply(MSG_NOT_IN_PAPERS, []), on_token)
                    return "ok", "refused_not_in_papers"
                if v.any_ok:
                    await self._deliver(trace, Reply(*render(v, passages)), on_token)
                    return "ok", "answered" if not v.dropped else "answered_partial"
                if round_no + 1 < rounds:
                    trace.regenerations += 1
                    messages = regenerate_messages(messages, text, v.problems())
            await self._deliver(trace, Reply(MSG_UNVERIFIED, []), on_token)
            return "ok", "refused_unverified"

        # 6. failure paths
        except DailyBudgetExhausted as e:
            await self._deliver(trace, self._passages_only(passages, e), on_token, counts_as_answer=False)
            return "rate_limited", f"daily_budget_exhausted_{e.kind}"
        except AdmissionRejected as e:
            wait = f" (estimated wait {e.estimated_wait_s:.0f} s)" if e.estimated_wait_s else ""
            await self._deliver(trace, Reply("The assistant is at its model-usage limit and cannot answer within "
                                             f"your time limit{wait}. Please retry shortly.", []),
                                on_token, counts_as_answer=False)
            return "rate_limited", e.reason
        except DeadlineExceeded as e:
            trace.event("deadline", where=str(e))
            await self._deliver(trace, Reply("This took too long and was stopped. Please retry.", []),
                                on_token, counts_as_answer=False)
            return "timeout", "deadline"
        except ProviderError as e:
            r = e.result
            if r.error_kind == RATE_LIMITED:
                status = "rate_limited"
            elif r.error_kind in (TIMEOUT_BEFORE_FIRST_TOKEN, TIMEOUT_AFTER_FIRST_TOKEN) or deadline.expired():
                status = "timeout"
            else:
                status = "error"
            await self._deliver(trace, Reply("The language model failed to answer. Please retry.", []),
                                on_token, counts_as_answer=False)
            return status, f"provider_{r.error_kind}"

    # ---------------------------------------------------------------------------------------
    async def _retrieve(self, query, setting, trace, deadline, *, pieces=None, rerank=None, fetch=None,
                        papers=None, span="retrieve") -> RetrievalResult:
        last: ToolError | None = None
        for attempt in range(2):
            timeout = deadline.cap(self.s.tool_timeout_s)
            if timeout <= 0.05:
                break
            with trace.span(span, attempt=attempt) as sp:
                try:
                    r = await self.tool(query, setting, pieces=pieces, rerank=rerank, timeout_s=timeout,
                                        fetch=fetch, papers=papers)
                    sp.meta.update(reranked=r.reranked, top_rerank=r.top_rerank, n=len(r.passages),
                                   **{k: round(v, 1) for k, v in r.timings.items()})
                    return r
                except ToolError as e:
                    sp.meta["error"] = type(e).__name__
                    last = e
        raise last or ToolTimeout("no time left for retrieval")

    async def _generate(self, messages, trace: RequestTrace, deadline: Deadline) -> str:
        est = count_prompt_tokens(messages) + self.s.expected_output_tokens
        self.retry.first_attempt()
        retries = 0
        while True:
            permit = await self.gate.acquire(est, deadline)
            trace.queue_wait_ms += permit.queue_wait_s * 1000
            if trace.queue_len_at_arrival is None:
                trace.queue_len_at_arrival = permit.queue_len_at_arrival
            call = self.provider.stream_chat(
                messages, max_tokens=self.s.max_output_tokens, timeout_s=deadline.remaining(),
                first_token_timeout_s=deadline.cap(self.s.first_token_timeout_s))
            err: ProviderError | None = None
            try:
                async for _ in call:   # whole answer is verified before anything is shown (D5)
                    pass
            except ProviderError as e:
                err = e
            finally:
                r = call.result
                if r.status_code == 429:
                    actual = 0                       # rejected requests consumed nothing (R2)
                elif r.tokens_in is not None:
                    actual = r.tokens_in + (r.tokens_out or 0)
                else:
                    actual = None
                permit.release(actual)
                self.limiter.observe(r)
                trace.tokens_in += r.tokens_in or 0
                trace.tokens_out += r.tokens_out or 0
                trace.attempts.append({
                    **r.to_log(), "queue_wait_ms": round(permit.queue_wait_s * 1000, 1),
                    "start_ms": round((r.t_start - trace.t0) * 1000, 1) if r.t_start else None})
            if err is None:
                return r.content
            d = self.retry.decide(r.error_kind, r.limit_kind, r.rate_limit.retry_after_s, retries,
                                  deadline.remaining(), self.s.expected_service_s)
            trace.attempts[-1]["retry_decision"] = {"retry": d.retry, "wait_s": round(d.wait_s, 3),
                                                    "reason": d.reason}
            if not d.retry:
                if r.limit_kind in ("RPD", "TPD"):
                    raise DailyBudgetExhausted(r.limit_kind, r.rate_limit.reset_requests_day_s)
                raise err
            retries += 1
            trace.retries += 1
            await asyncio.sleep(d.wait_s)

    @staticmethod
    def _passages_only(passages, e: DailyBudgetExhausted) -> Reply:
        when = (f"in about {e.reset_s / 3600:.1f} h" if e.reset_s else "when the provider's daily budget resets")
        lines = [f"Today's model budget is used up, so I can't write an answer (it should return {when}). "
                 "These are the most relevant passages from the papers:"]
        lines += [f"[{i}] {p.chunk.text[:400]}" for i, p in enumerate(passages[:3], start=1)]
        return Reply("\n".join(lines), [f"[{i}] {p.citation()}" for i, p in enumerate(passages[:3], start=1)])
