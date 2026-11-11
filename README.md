# The Relay

A small, fault-tolerant research assistant. It answers engineers' questions **only** from three arXiv papers, cites
paper, section and page for every statement, and says "the papers don't say" instead of guessing. It runs on a real
hosted model (Groq free tier, `openai/gpt-oss-20b`) and on a calibrated **stand-in model service** used to test
behaviour under load, failures and rate limits that the free tier cannot safely reproduce.

Built for the "The Relay" assignment (`The_Relay_Assignment_Brief.pdf`).

**Status (11 Nov 2026):** pipeline, stand-in, load experiments, reporting and 111 tests are done. The real-Groq
retrieval evaluation (Table 3) and the real steady-load baseline are scheduled for 12 Nov on a fresh daily quota
window; hand grading, Vaani feedback, the conversation history and the final report are pending (see
[Status and pending work](#status-and-pending-work)).

---

## Contents

1. [What it does](#what-it-does)
2. [The paper library](#the-paper-library)
3. [How it works](#how-it-works)
4. [Setup](#setup)
5. [Configuration](#configuration)
6. [Running the assistant](#running-the-assistant)
7. [The stand-in model service](#the-stand-in-model-service)
8. [Experiments](#experiments)
9. [Results and reproducibility](#results-and-reproducibility)
10. [Tests](#tests)
11. [Repository layout](#repository-layout)
12. [Documentation](#documentation)
13. [Status and pending work](#status-and-pending-work)
14. [Known limitations](#known-limitations)
15. [Credits](#credits)

---

## What it does

- **Answers from the papers only.** Retrieval finds passages; the model must cite them as `[P1]`, `[P2]`...; a
  rule-based verifier checks **every sentence** before anything is shown: the cited passage was actually sent, it
  comes from the paper the question names, every number in the sentence appears in it, and enough of the sentence's
  words appear in it. Unsupported sentences are removed; if nothing survives, the answer is regenerated once or refused.
- **Refuses when the papers don't say.** Clearly off-topic questions are refused before any model call (re-rank score
  below −8, zero tokens spent); otherwise the model may answer `NOT_IN_PAPERS`.
- **Stays inside provider limits.** A bounded FIFO queue, a concurrency cap, and our own token-aware rate limiter pace
  calls *before* sending, learn changed limits from response headers, and turn a request away immediately when its
  estimated wait cannot fit its 30 s deadline.
- **Fails gracefully.** Bounded, jittered, deadline-aware retries with a shared retry budget; degraded mode (fewer
  passages) when the per-minute token budget is low; a passages-only reply with the reset time when the daily quota is
  used up; cancellation when the person gives up.
- **Measures everything.** One row per request in `results.csv` (exactly the 12 required columns) plus a full trace
  (stages, every provider attempt, headers) in `results/traces.jsonl`.

## The paper library

Exactly these three versions, downloaded by `scripts/fetch_papers.py` and pinned by SHA-256 in `papers/manifest.json`:

| Id | Paper | Authors | arXiv |
|---|---|---|---|
| `attention` | Attention Is All You Need | Ashish Vaswani, Noam Shazeer, Niki Parmar, Jakob Uszkoreit, Llion Jones, Aidan N. Gomez, Łukasz Kaiser, Illia Polosukhin (Google Brain, Google Research, University of Toronto) | [1706.03762v7](https://arxiv.org/abs/1706.03762v7) |
| `moshi` | Moshi: a speech-text foundation model for real-time dialogue | Alexandre Défossez, Laurent Mazaré, Manu Orsini, Amélie Royer, Patrick Pérez, Hervé Jégou, Edouard Grave, Neil Zeghidour (Kyutai) | [2410.00037v1](https://arxiv.org/abs/2410.00037v1) |
| `hnet` | Dynamic Chunking for End-to-End Hierarchical Sequence Modeling (H-Net) | Sukjun Hwang, Brandon Wang, Albert Gu (Carnegie Mellon University, Cartesia AI) | [2507.07955v2](https://arxiv.org/abs/2507.07955v2) |

Titles and authors are copied from the papers' first pages. The PDFs belong to their authors; they are used here only
as the assistant's reference library.

## How it works

```
question ─► 1 Intake ─► 2 Analyze ─► 3 Retrieve (TOOL) ─► 4 Decide ──┬─► refuse (no model call)
                                          ▲                          ├─► widen search once (LOOP)
                                          └──────────────────────────┘
                                                                     ▼
            6 Deliver ◄── 5b Verify ◄── 5a Generate (MODEL, via the gate) ◄─┘
                              └── regenerate once if nothing verifies (LOOP)
```

| Step | What happens | Code |
|---|---|---|
| 1 Intake | request id, 30 s deadline, arrival time | `app/pipeline/orchestrator.py` |
| 2 Analyze | validate; detect named papers ("H-Net", "Helium", "original Transformer"...) | `app/pipeline/analyzer.py` |
| 3 Retrieve | BM25 + cross-encoder re-rank, per-paper search for comparisons; timeout, 1 retry | `app/pipeline/retrieval.py` |
| 4 Decide | refuse / widen once / degraded mode | `app/pipeline/orchestrator.py` |
| 5a Generate | streaming call through the model gate (queue → limiter → slot), retries | `app/infra/admission.py`, `retry.py`, `rate_limiter.py` |
| 5b Verify | per-sentence citation, paper, number and word-overlap checks | `app/pipeline/verifier.py` |
| 6 Deliver | stream the verified answer with numbered sources | `app/main.py` |

**Why verify the whole answer before streaming it?** Groq generates ~1,000 tokens/s, so waiting for a complete
150-token answer costs ~0.15 s, and it guarantees no unverified sentence is ever shown (decision D5). `first_token_ms`
therefore means: request arrival → first token of the *verified* answer, queue wait included.

**Why deterministic analysis and verification?** The binding limit is 8,000 tokens per minute: at ~1,650 tokens per
answer that is ~4–5 answers per minute. An extra model call per question (query rewriting, LLM judge) would halve
capacity (D8).

Each request ends with exactly one status:

| Status | Meaning |
|---|---|
| `ok` | answered, partly answered, or correctly refused ("the papers don't say") |
| `rate_limited` | turned away at arrival (queue full / wait exceeds deadline), daily quota used up, or 429 after retries |
| `timeout` | deadline passed, provider timed out, or the person gave up |
| `error` | provider or tool failure, invalid input |

Full design: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Setup

Requirements: Windows, macOS or Linux; Python 3.12+ (developed and tested on Python 3.13.6); internet access for the
first run (paper download, ~90 MB re-ranker model).

```powershell
python -m pip install -r requirements.txt
copy .env.example .env            # then put your own GROQ_API_KEY in .env — never commit it
python scripts/fetch_papers.py    # downloads the three PDFs, checks SHA-256
python -m app.ingest.build_index  # parses the PDFs, builds the three retrieval indexes
```

The cross-encoder re-ranker is downloaded on first use into `.cache/fastembed` (ignored by git).

> A `pyproject.toml` from `uv init` exists but its dependency list is empty; `requirements.txt` is the source of
> truth for now (`uv add -r requirements.txt` would sync them — pending decision).

## Configuration

Environment variables (read from `.env` by `app/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | your own Groq key; required only for real runs; never logged or written to results |
| `GROQ_BASE_URL` | `https://api.groq.com/openai/v1` | Groq's OpenAI-compatible API |
| `MODEL` | `openai/gpt-oss-20b` | model id (same for Groq and the stand-in) |
| `STANDIN_BASE_URL` | `http://127.0.0.1:8100/openai/v1` | stand-in service |
| `BACKEND` | `standin` | for `app.main`: `standin` or `real` |

Everything else is an explicit default in `app/config.py` (deadline 30 s, queue 50, 4 model calls in flight, 2 retries,
retry budget 20 %, first-token timeout 6 s, refuse below re-rank −8, degraded below 25 % TPM headroom, ...), so every
run records the exact configuration it used (`results/runs/<run_id>/meta.json`).

Retrieval settings (compared in Table 3):

| Setting | Piece size (tokens) | Overlap | Pieces in prompt | Candidates before re-rank | Re-rank |
|---|---|---|---|---|---|
| `small_k6` | 200 | 40 | 6 | 6 | no |
| `medium_k4_rr` (default) | 350 | 50 | 4 | 20 | yes |
| `large_k2_rr` | 700 | 80 | 2 | 20 | yes |

## Running the assistant

```powershell
python -m app.simulator.server --profile normal --port 8100              # terminal 1 (stand-in)
$env:BACKEND = "standin"; python -m uvicorn app.main:app --port 8000     # terminal 2
# or, against Groq (spends quota):
$env:BACKEND = "real";    python -m uvicorn app.main:app --port 8000
```

- `POST /ask` with `{"question": "...", "retrieval_setting": "medium_k4_rr", "deadline_s": 30}` streams server-sent
  events: `{"type":"token","text":...}` chunks of the verified answer, then one
  `{"type":"done","status":...,"outcome":...,"sources":[...],"first_token_ms":...,"total_ms":...}`.
- `GET /health` shows queue length, calls in flight, per-minute token headroom and whether the daily quota is blocked.
- Disconnecting cancels the request (recorded as `timeout` / `client_gave_up`).
- Every request appends one row to `results.csv` and one trace to `results/traces.jsonl`.

Quick end-to-end check without quota: `python -m experiments.pipeline_smoke` (all questions through an in-process
stand-in; writes nothing).

## The stand-in model service

A FastAPI service that speaks Groq's wire format (same endpoint, SSE chunks, `usage`, `x-ratelimit-*` and
`retry-after` headers, 429 body), so the pipeline uses **one client** for both backends.

```powershell
python -m app.simulator.calibration                  # fit latency/limits from the real probe logs
python -m app.simulator.server --profile normal --port 8100 [--seed 0] [--scale 1.0]
python -m experiments.standin_check                  # compare stand-in vs real on the same probe shapes
```

- Calibrated from 107 real requests: first byte ≈ 498 ms + 27 ms per 1K input tokens, ~4 % slow starts, ~1,090
  tokens/s, RPM 30 (burst 22), TPM 8,000 checked on the prompt at admission, RPD 1,000.
- Assumed (not measurable on the free tier): behaviour under heavy concurrency, failure rates, daily refill.
- Profiles: `normal`, `unlimited`, `high_latency`, `flaky`, `rate_limited` (limits halved), `daily_exhausted`,
  `heavy_concurrency`, `overload`, `erroring` (20 % of calls fail with a 500; assumed rate, for the retry runs).
- Admin: `GET /admin/state`, `POST /admin/profile {"name": "...", "keep_state": true}` (change the rules mid-run while
  keeping usage), `POST /admin/reset`, `GET /admin/log`.

Where it is least like Groq: [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md#where-the-stand-in-is-least-like-groq).

## Experiments

Real runs never start without `--yes`; without it every real script prints a token estimate built from the exact
prompts it would send (`experiments/budget.py`). Ordinary tests never use quota.

| Command | What | Quota |
|---|---|---|
| `python -m experiments.real_probe budget\|smoke\|latency\|rpm\|tpm\|concurrency [--yes]` | real limit and latency probes (R0–R4) | yes |
| `python -m experiments.analyze_probes` | summarise probe logs → `results/real_probe/summary.json` | no |
| `python -m experiments.retrieval_offline` | retrieval hit rate per setting, no model | no |
| `python -m experiments.load_test all` | five stand-in load scenarios (~32 min) | no |
| `python -m experiments.load_test retries` | same load with our retry policy, no retries, naive retries; spare and busy (~35 min) | no |
| `python -m experiments.load_test <scenario> --seed N` | one scenario | no |
| `python -m experiments.load_test steady_real --backend real [--yes]` | real steady baseline (12 questions) | ~21K tokens |
| `python -m experiments.retrieval_eval [--yes]` | 21 questions × 3 settings on Groq + grading sheets | ~96–114K tokens |
| `python -m experiments.apply_grades [--check]` | copy hand grades into `results.csv` | no |
| `python -m experiments.slow_request --source stand-in\|real` | where one slow request's time went | no |
| `python -m experiments.inspect_run [run_id]` | quick per-run breakdown | no |

Load scenarios (stand-in, deadline 30 s, 20 % of simulated people give up after 8–15 s — an assumption):

| Scenario | Load |
|---|---|
| `steady` | Poisson 4/min for 10 min |
| `burst` | 2/min background + 20 at once at t = 60 s |
| `overload` | Poisson 20/min for 5 min |
| `limit_halved` | constant 5/min for 8 min; provider halves RPM/TPM without notice at 4 min |
| `daily_exhausted` | 6/min for 5 min with 20 requests left in the daily quota |
| `errors_policy` / `errors_none` / `errors_naive` | 4/min for 6 min, 20 % of model calls fail; identical arrivals, only the retry behaviour differs |

Questions: 21 self-written questions in `data/questions.json` (16 answerable incl. one cross-paper, 5 unanswerable
traps), each with expected facts and locations.

## Results and reproducibility

```powershell
python -m experiments.runs                  # rebuild results.csv + results/traces.jsonl from the selected runs
python -m app.evaluation.report             # Tables 1, 2a, 2b, 3 -> results/tables/
python -m app.evaluation.report --check     # regenerate and compare; prints "reproducible"
```

- Every run writes to its own `results/runs/<run_id>/` (meta, CSV, traces, per-second timeline, stand-in log) and is
  never edited except for copied hand grades. `results/selected_runs.json` names the run used per scenario; superseded
  and invalid runs stay as evidence (D15).
- Real and stand-in rows are kept apart by the `source` column and labelled in every table.
- Percentiles are nearest-rank (always an observed value). First-token percentiles use `ok` requests; total-time and
  outcome percentages use every request. Missing evidence is written `not measured`.
- Cost per 1,000 answers uses Groq's list price for `openai/gpt-oss-20b` (USD 0.075 / 1M input, 0.30 / 1M output); the
  free tier itself costs nothing.

Measured so far (from the generated tables; details in `results/tables/` and `docs/EXPERIMENTS.md`):

- **Groq free tier, `openai/gpt-oss-20b`:** 30 requests/min (429 names "RPM"), 8,000 tokens/min (header and 429),
  1,000 requests/day (header; not reached on purpose), 200,000 tokens/day (documentation only), ≥ 8 requests in flight
  succeeded, 429 returned in ~60–80 ms with `retry-after` 2 s, recovery in ~3.7–4.5 s. First token p50 532 / 578 /
  719 ms at ~310 / 1,258 / 2,548 input tokens.
- **Stand-in load scenarios:** no request received a 429 — the client paced itself and learned the halved limit from
  headers. Burst: 18/29 answered, 11 turned away at arrival. Overload (17.4/min): 57/87 answered, 16 turned away, 14
  gave up while queued; first token p50 16.1 s. Slowest answered request: 29.0 s, 80.9 % of it queue wait.
- **Retries (stand-in, calls failing at random):** with spare capacity retrying only helped; when busy it turned 6
  errors into 4 answers and 2 refusals at arrival, and made one person wait 3.8 s longer for the same non-answer.
- **Stand-in calibration bug found and fixed:** it streamed ~4x slower than calibrated after the first token (Windows
  timer rounding); all stand-in load runs were re-run.
- **Table 3 (retrieval settings on real Groq): not measured yet.** The first real attempt was invalid (degraded mode
  was active) and is excluded; see `docs/EXPERIMENTS.md`.

## Tests

```powershell
python -m pytest        # 111 tests, ~50 s; never uses Groq quota (tests marked `real` are skipped)
```

Covered: request validation, PDF parsing and chunking, BM25 and retrieval hits, citation/number/paper verification,
refusals, client rate limiter, token budget, queue capacity and early rejection, concurrency cap, deadlines and
first-token timeout, retry rules and retry budget, cancellation, SSE streaming API, daily quota exhaustion (both
variants), all stand-in profiles over real HTTP, load-generator reproducibility, results assembly, report tables and
the slow-request breakdown.

## Repository layout

| Path | Contents |
|---|---|
| `app/config.py` | settings, retrieval settings, CSV schema, prices |
| `app/main.py` | FastAPI app: `POST /ask` (SSE), `GET /health` |
| `app/ingest/` | PDF parsing (PyMuPDF), chunking, BM25, index builder |
| `app/pipeline/` | orchestrator, analyzer, retrieval tool, prompts, verifier |
| `app/providers/` | one OpenAI-compatible client (raw `httpx`) for Groq and the stand-in |
| `app/infra/` | model gate (queue + concurrency), client rate limiter, token bucket, retries, deadlines, metrics |
| `app/simulator/` | stand-in service, behaviour models, profiles, calibration, in-process runner |
| `app/evaluation/` | percentile definition, report tables |
| `experiments/` | real probes, load tests, scenarios, retrieval evaluation, grading, budget, run bookkeeping |
| `scripts/` | `fetch_papers.py`, `find_in_papers.py` (regex search over the parsed papers) |
| `data/` | `questions.json`, stand-in calibration, retrieval indexes (generated) |
| `papers/` | the three PDFs + `manifest.json` |
| `results/` | real probe logs, `groq_spend.jsonl` (every real call), runs, grading sheets, tables |
| `results.csv` | assembled request-level results (12 required columns) |
| `tests/` | pytest suite |
| `docs/` | design, decisions, experiments, requirement mapping |

## Documentation

| Document | Contents |
|---|---|
| [`docs/ASSIGNMENT_ANALYSIS.md`](docs/ASSIGNMENT_ANALYSIS.md) | every requirement of the brief and its status |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | pipeline, request lifecycle, admission control, retries, statuses |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | decisions D1–D16 with alternatives and trade-offs |
| [`docs/EXPERIMENTS.md`](docs/EXPERIMENTS.md) | methodology, real measurement log, calibration, scenarios, findings, incidents |
| `docs/FINAL_REPORT.md` | two-page report — pending |

## Status and pending work

Done: paper library, ingestion and retrieval, Groq provider and real probes (R0–R4), stand-in and calibration,
orchestration and fault tolerance, five stand-in load scenarios, report tables (1, 2a, 2b), slow-request breakdown
(stand-in), daily-quota user experience, 111 tests.

Pending:

| Item | Who |
|---|---|
| Clean real retrieval evaluation (R5) and `steady_real` baseline (R6), 12 Nov on a fresh daily window | assistant runs, with approval |
| Verify the 21 questions in `data/questions.json` (`user_verified`) | Satyabrata |
| Hand-grade the answers of the clean R5 run in `results/grading/*.csv` (not `invalid_r5_attempt1/`), then `python -m experiments.apply_grades` | Satyabrata |
| Real slow-request example (`--source real`) and Table 3 | after R5/R6 |
| Optional: RPD idle-refill check (one tiny request after ≥ 1 h idle) | Satyabrata's approval |
| Visit https://www.vaani.dev/ and write the Vaani feedback | Satyabrata |
| Export the AI-assistant conversation history | Satyabrata |
| Final report (≤ 2 pages): prompts, mistake caught, decision overruled, own contribution | Satyabrata + assistant draft |
| Decide: `uv` dependencies, Groq org id in raw probe logs, whether to commit the brief PDF | Satyabrata |

## Known limitations

- Verification is rule-based: it catches wrong citations, wrong papers, invented numbers and unsupported wording, but
  cannot judge paraphrased meaning (D8).
- The stand-in's answers are extractive, so stand-in runs say nothing about answer quality or refusals (D11).
- Behaviour under heavy concurrency, failure rates and the daily refill are assumed in the stand-in, not measured.
- The cross-encoder re-ranker runs on CPU (~1.0–1.4 s per query) and is the slowest stage when the queue is empty.
- A request turned away for capacity has already paid for retrieval; checking admission first would save that CPU.
- Patience of simulated people (20 % give up after 8–15 s) is an assumption.

## Credits

- **Project author:** Satyabrata Mishra.
- **Built with AI assistance:** developed with an AI coding assistant (Cursor agent) under Satyabrata's direction;
  the conversation history is a required deliverable (pending export). Decisions marked "(User)" in
  `docs/DECISIONS.md` were made by Satyabrata; those marked "(Assistant)" were proposed by the assistant.
- **Assignment:** "The Relay" brief (`The_Relay_Assignment_Brief.pdf`).
- **Papers:** the authors listed in [The paper library](#the-paper-library); PDFs from [arXiv](https://arxiv.org).
- **Model and inference:** [Groq](https://groq.com) (free tier, OpenAI-compatible API) serving OpenAI's open-weight
  `gpt-oss-20b`.
- **Re-ranker:** `Xenova/ms-marco-MiniLM-L-6-v2` (an ONNX export of the MS MARCO MiniLM cross-encoder) via
  [fastembed](https://github.com/qdrant/fastembed).
- **Libraries:** [httpx](https://www.python-httpx.org/) (HTTP client), [FastAPI](https://fastapi.tiangolo.com/) and
  [Uvicorn](https://www.uvicorn.org/) (API and stand-in), [PyMuPDF](https://pymupdf.readthedocs.io/) (PDF parsing),
  [fastembed](https://github.com/qdrant/fastembed) (ONNX re-ranker), `py_rust_stemmers` (Snowball stemming, installed
  with fastembed), [NumPy](https://numpy.org/), [python-dotenv](https://github.com/theskumar/python-dotenv),
  [pytest](https://pytest.org/) and [pytest-asyncio](https://github.com/pytest-dev/pytest-asyncio). Exact versions in
  `requirements.txt`.
- **Algorithms:** BM25 ranking (Robertson and Zaragoza) and full-jitter exponential backoff, implemented in this repo.
