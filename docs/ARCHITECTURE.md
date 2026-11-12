# Architecture — The Relay

Status: **implemented and tested** (112 tests, none use Groq quota). Numbers marked *(doc)* come from Groq's
documentation; numbers marked *(est)* are design-time estimates. Measured values are in `docs/EXPERIMENTS.md`.

## 1. The constraint that shapes everything

Primary model: `openai/gpt-oss-20b` on the Groq free tier: 30 RPM, 8K TPM, 1K RPD, 200K TPD *(doc)*.

One answer ≈ system prompt (≈400 tokens) + retrieved passages (4 x ≈300 = ≈1,200) + question (≈50)
+ output (≈300, plus reasoning tokens) ≈ **2K tokens** *(est)*. Measured since: ≈1,480 tokens per answer in the
stand-in `steady` run, ≈870 in degraded mode (Table 2b); real answers used 60–230 output tokens.

- Tokens per minute: 8,000 / 2,000 ≈ **4 answers per minute**. The 30-RPM limit is never the one we hit; **TPM binds first**.
- Tokens per day: 200,000 / 2,000 ≈ **100 answers per day** for the *whole account*, including our own experiments.

Consequences:
1. Retrieval size is not just a quality knob — it **is** the throughput knob. Halving the passage tokens nearly doubles capacity.
2. Every avoidable model call (query rewriting, LLM-as-judge verification) costs real capacity, so those steps are deterministic code.
3. Admission control must reason in **tokens**, not requests.
4. The real-API experiment budget is tight; it is planned in `docs/EXPERIMENTS.md` before any run.

## 2. Pipeline (6 steps, 1 model call normally, at most 2)

```
 client ──► [1 Intake] ──► [2 Analyze] ──► [3 Retrieve: TOOL] ──► [4 Decide: BRANCH] ──┬─► refuse (no model call)
                                               ▲                         │             ├─► degraded answer path
                                               └── re-retrieve once ◄────┤ (LOOP)      └─► queue for model
                                                                         ▼
                              [6 Deliver] ◄── [5b Verify] ◄── [5a Generate: MODEL CALL, streaming]
                                                   │
                                                   └── regenerate once if nothing verifiable (LOOP, budget permitting)
```

| Step | What it does | Kind | Can fail by |
|---|---|---|---|
| 1 Intake | request id, deadline (default 30 s), arrival timestamp (`orchestrator.Pipeline.handle`) | app code | — |
| 2 Analyze | validate length, detect named papers by alias ("H-Net", "Helium", "original Transformer"...); "Transformer" alone means the Attention paper only if no other paper is named (`analyzer.py`) | app code | invalid input → status `error` |
| 3 Retrieve | BM25 (+ cross-encoder re-rank); a comparison question naming 2+ papers searches each paper separately (`retrieval.py`) | **tool call**, timeout 4 s, injectable delay/failure | 1 retry, then status `error` (`tool_failed`) |
| 4 Decide | refuse without a model call if top re-rank score < −8; if < 0, search once more with twice the candidates (LOOP); degraded mode if TPM headroom < 25 % or ≥ 4 requests wait | app code | — |
| 5a Generate | streaming chat completion with numbered passages; must cite `[Pn]` or reply `NOT_IN_PAPERS` | **model call** through the gate (§3) | 429, 5xx, timeout, slow |
| 5b Verify | per sentence: cites a passage we sent; cited passage is from the named paper; every multi-digit/decimal number appears in it; ≥ 40 % of content words appear in it (`verifier.py`) | app code | failing sentences dropped; none left → regenerate once (LOOP, not in degraded mode), else refuse |
| 6 Deliver | stream the verified text, `[Pn]` renumbered `[1]..` with "Attention (arXiv 1706.03762v7), §3.2.1, p.4" sources | app code | client gone → task cancelled, upstream stream closed |

### Key design decision: verify the whole answer, then stream it (D5)

The brief requires checking the reply before sending it, and also cares about time to first token. Sentence-by-sentence
release was the first plan; measurement changed it: gpt-oss-20b generates ≈1,000 tokens/s on Groq, so a whole
150-token answer takes ≈0.15 s after its first token. Waiting for the complete answer costs that little, and lets the
verifier see every sentence (and refuse or regenerate) before anything is shown.

Therefore `first_token_ms` in `results.csv` = **request arrival → first token of the verified answer** (queue wait,
retrieval and verification included). It is filled only for status `ok`; the provider's own first-token time is in the
trace for every attempt, so the gap can be explained.

### Statuses (exactly one per request)

| status | outcomes (trace field `outcome`) |
|---|---|
| `ok` | `answered`, `answered_partial` (some sentences dropped), `refused_no_evidence` (decided before the model), `refused_not_in_papers` (model said so), `refused_unverified` (nothing survived verification) |
| `rate_limited` | `queue_full`, `wait_exceeds_deadline` (turned away at arrival), `daily_budget_exhausted_RPD/TPD` (passages-only reply), `provider_rate_limited` (429 after retries) |
| `timeout` | `deadline` (expired while waiting or generating), `provider_timeout_*`, `client_gave_up` (cancelled) |
| `error` | `provider_server`, `provider_connection`, `provider_client`, `tool_failed`, `invalid_input`, `unexpected_error` |

## 3. Request lifecycle and admission control

```
arrive ─► admission check ─► bounded FIFO queue ─► concurrency slot ─► rate limiter (RPM + TPM + daily) ─► provider
            │                       │                                                │
            │ reject now if the     │ drop if its deadline                           │ 429 → classify → retry with
            │ estimated wait > the  │ passes while waiting,                          │ backoff + jitter, only if the
            │ remaining deadline    │ or the client has gone                         │ remaining deadline allows
```

- **Bounded queue** (`queue_max` 50). When full: immediate `rate_limited` reply — never unbounded growth.
- **Early rejection**: on arrival the gate estimates the wait from tokens already queued, the TPM/RPM refill rates, any retry-after cooldown and the slots ahead; if wait + one service time exceeds the remaining deadline, the request is turned away now rather than failing late.
- **Concurrency**: `asyncio.Semaphore` on in-flight model calls (default 4); waiters are served FIFO.
- **Rate limiter** (`infra/rate_limiter.py`): token buckets for RPM (burst 22, fitted in R2) and TPM, plus daily counters. It reserves *estimated* tokens (prompt + 150 output) before a call and settles to *actual* usage after; a 429 settles to 0 (rejected calls cost nothing, R2). Headers can only lower our view (they arrive out of order under concurrency, R4), except a changed TPM *limit*, which is adopted. A 429 naming RPD/TPD blocks all further calls.
- **Deadlines**: each request has one deadline; every stage receives the remaining time; the first-token timeout is min(6 s, remaining).
- **Cancellation**: if the client disconnects, the request task is cancelled, which closes the upstream stream (the stand-in confirmed it stops charging tokens) and records `timeout / client_gave_up`.
- **Degraded mode** (TPM headroom < 25 % or ≥ 4 waiting): 2 passages instead of 4 and no regeneration. Re-ranking stays on: it costs CPU, not provider tokens, and its score is what refuses off-topic questions (the first smoke run answered "capital of France" from Moshi's demo transcripts when degraded mode also dropped re-ranking). The answer is still verified.
- **Re-ranker queue**: the CPU re-ranker (≈1.2 s/query) runs one at a time with at most 2 waiting; beyond that, the query uses BM25 order (counted in `RetrievalTool.rerank_skipped`).
- **Daily budget exhausted**: no model calls. The reply says that the provider's daily quota is used up, that no answer can be written, that nothing shown is generated, and when to retry (the provider's reset header as a UTC clock time, or "unknown" for the token limit); then the top 3 retrieved passages with their locations, labelled "relevant passages, not an answer". Status `rate_limited`. Exact text in EXPERIMENTS.md.

Who is affected at each limit (observed in the stand-in `burst`, `overload`, `limit_halved` and `daily_exhausted` runs, EXPERIMENTS.md):
| Situation | Delayed | Dropped | Weaker answer |
|---|---|---|---|
| Near per-minute limit | requests whose estimated wait fits their deadline | requests whose deadline cannot be met (rejected at admission, not after waiting) | new requests while headroom is low (degraded retrieval) |
| Per-day limit gone | nobody (waiting cannot help) | — | everyone: passages-only reply + reset time |

## 4. Retries

- Retry: 429 for RPM/TPM (wait = `retry-after` + 0–0.25 s jitter), 5xx, connection errors, timeouts. Because the whole answer is buffered before release (D5), a failure after the first token is still invisible to the person and is retried too.
- Never retry: other 4xx; 429 naming RPD/TPD (waiting cannot help); when wait + one expected call does not fit the remaining deadline.
- Exponential backoff with full jitter (0.5 s × 2^n, cap 8 s), max 2 retries per model call. Shared retry budget: each first attempt earns 0.2 retries, each retry spends 1 (cap 10), so a provider-wide failure cannot multiply load by 3.
- Each retry goes back through the gate (queue, limiter, slot), so retries are paced like new work.
- `RetryPolicy(naive=True)` (immediate, ignores retry-after and budget) exists only for the "retries made it worse" experiment.

## 5. Model backends — one client, two servers

The stand-in speaks **Groq's OpenAI-compatible wire format**: same endpoint path, same SSE streaming chunks, same `usage` block, same `x-ratelimit-*` and `retry-after` headers, same 429 body shape. The pipeline uses one HTTP provider class and switches backend only by `base_url`. This means the exact code path used in load tests is the one used against Groq.

We call the REST API with `httpx` rather than the `groq` SDK, because the SDK retries automatically by default (which would hide retries from our measurements) and makes raw headers harder to read on streams.

Stand-in behaviour (each knob calibrated from real probes or marked assumed):
- latency = sampled TTFT + output tokens / sampled tokens-per-second, from fitted real distributions
- load factor: latency multiplied by `1 + alpha * in_flight` (alpha assumed — the free tier cannot be loaded enough to measure it)
- RPM / TPM / RPD / TPD limits with Groq-style headers and 429s; configurable (halved-limit profile)
- random failure rate (5xx), slow-success rate (long tail), mid-stream disconnects
- profiles: `normal`, `high_latency`, `flaky`, `rate_limited`, `daily_exhausted`, `heavy_concurrency`, `overload`,
  `erroring` (20 % 500s, for the retry runs), `unlimited`

## 6. Retrieval

- Parse with PyMuPDF page by page (pypdf splits words, e.g. "T ext"); detect section headings by numbering pattern + font size; tag every chunk with paper, version, section, page range.
- Exclude the reference list from the index by default (see the naive-retriever failure modes in EXPERIMENTS.md).
- BM25 implemented in-repo (about 40 lines, no extra dependency); optional cross-encoder re-ranker via `fastembed` (ONNX, no PyTorch).
- Settings vary piece size (tokens), pieces fetched, and re-rank on/off.

## 7. Instrumentation

- `results.csv`: exactly the 12 required columns, one row per user request.
- `results/traces.jsonl`: one JSON object per request with stage spans and per-attempt provider details, joined on `request_id`.
- `results/runs/<run_id>/meta.json`: config + seed + profile + git commit for every experiment run.
- Report tables are produced only by `app/evaluation/report.py` from these files — never typed by hand.

## 8. Directory structure

```
.
├── app/
│   ├── config.py               # settings from env, retrieval settings, defaults
│   ├── main.py                 # FastAPI app: POST /ask (SSE stream)
│   ├── ingest/                 # parse_pdf.py, chunker.py, bm25.py, build_index.py
│   ├── pipeline/               # orchestrator.py (steps 1, 4, 6 + generate loop), analyzer.py,
│   │                           # retrieval.py, prompts.py, verifier.py
│   ├── providers/              # base.py, openai_compat.py (Groq and stand-in)
│   ├── infra/                  # admission.py (queue + semaphore), rate_limiter.py, token_bucket.py,
│   │                           # retry.py, deadline.py, metrics.py
│   ├── simulator/              # server.py, behavior.py, profiles.py, calibration.py, runner.py
│   └── evaluation/             # report.py (tables, --check), stats.py (nearest-rank percentiles)
├── experiments/                # real_probe.py, analyze_probes.py, standin_check.py, scenarios.py, load_test.py,
│                               # retrieval_offline.py, retrieval_eval.py, budget.py, apply_grades.py, runs.py,
│                               # inspect_run.py, slow_request.py, pipeline_smoke.py
├── data/                       # questions.json, standin_calibration.json, built indexes (generated)
├── papers/                     # the three PDFs + manifest.json (sha256)
├── results/                    # runs/<run_id>/, real_probe/, grading/, tables/, selected_runs.json
├── scripts/                    # fetch_papers.py, find_in_papers.py
├── tests/
├── docs/
├── results.csv
├── requirements.txt, .env.example, .gitignore, README.md
```

Differences from the suggested layout, and why:
- One `openai_compat.py` provider instead of separate Groq and simulator providers: the stand-in mimics Groq's protocol, so one client exercises the real code path in every test.
- `ingest/` split out of `pipeline/`: indexing happens offline, once per retrieval setting.
- `context_manager.py` + budget checks became the decide step inside `orchestrator.py` (about 30 lines; a separate module added indirection without reuse). Generation lives there too, because its retry loop needs the trace, gate and deadline.
- No `request_queue.py` / `concurrency.py` split: one `admission.py` owns queue + semaphore, so queue length and in-flight count are measured in one place.
