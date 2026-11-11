# Architecture — The Relay

Status: **design (Phase 1)**. Numbers marked *(doc)* come from Groq's documentation and are not yet measured;
numbers marked *(est)* are estimates to be replaced by measurements.

## 1. The constraint that shapes everything

Primary model: `openai/gpt-oss-20b` on the Groq free tier: 30 RPM, 8K TPM, 1K RPD, 200K TPD *(doc)*.

One answer ~= system prompt (~400 tokens) + retrieved passages (4 x ~300 = ~1,200) + question (~50)
+ output (~300, plus reasoning tokens) ~= **2K tokens** *(est)*.

- Tokens per minute: 8,000 / 2,000 ~= **4 answers per minute**. The 30-RPM limit is never the one we hit; **TPM binds first**.
- Tokens per day: 200,000 / 2,000 ~= **100 answers per day** for the *whole account*, including our own experiments.

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
| 1 Intake | request id, validation, deadline (default 30 s), arrival timestamp | app code | invalid input → 400 |
| 2 Analyze | normalize question, extract key terms, detect paper names/aliases (e.g. "H-Net" → Dynamic Chunking paper), flag obvious out-of-scope | app code | — |
| 3 Retrieve | BM25 over the chunk index (+ optional cross-encoder re-rank); returns passages with paper/section/page | **tool call** with timeout + injectable latency/failure | tool timeout → 1 retry, then refuse |
| 4 Decide | branch on retrieval confidence and budget: refuse now / re-retrieve with expanded query (once) / proceed full / proceed degraded / wait / reject | app code | — |
| 5a Generate | streaming chat completion with numbered passages; must cite `[P3]`-style ids or emit `NOT_IN_PAPERS` | **model call** | 429, 5xx, timeout, slow |
| 5b Verify | per sentence: cited id must be a passage we sent; numbers in the sentence must appear in the cited passage; minimum content-word overlap | app code | unsupported sentence → withheld |
| 6 Deliver | stream verified sentences, map `[P3]` → "Attention Is All You Need, §3.2.1, p.4" | app code | client gone → cancel upstream |

### Key design decision: verify-then-release, sentence by sentence

The brief requires checking the reply before sending it, and also cares about time to first token. These pull against each other:

| Option | User TTFT | Can a wrong claim reach the user? |
|---|---|---|
| Stream raw model tokens | best | yes |
| Buffer the whole answer, verify, then send | worst (= full generation time) | no |
| **Release each sentence once its citation verifies** (chosen) | model TTFT + one sentence | no unverified sentence is ever shown |

Therefore `first_token_ms` in `results.csv` means: **request arrival → first verified token delivered to the user** (queue wait included). The provider's own TTFT is recorded separately in the trace, so we can show where the gap comes from.

If a sentence fails verification it is withheld and the reply ends with a note saying which claim was dropped. If *no* sentence verifies, the system regenerates once (if deadline and budget allow), otherwise it refuses.

## 3. Request lifecycle and admission control

```
arrive ─► admission check ─► bounded priority queue ─► concurrency slot ─► rate limiter (RPM + TPM + daily) ─► provider
            │                       │                                                │
            │ reject now if the     │ drop if its deadline                           │ 429 → classify → retry with
            │ estimated wait > the  │ passes while waiting,                          │ backoff + jitter, only if the
            │ remaining deadline    │ or the client has gone                         │ remaining deadline allows
```

- **Bounded queue** (size configurable, default 50). When full: immediate rejection with a clear message — never unbounded growth.
- **Concurrency**: `asyncio.Semaphore` on in-flight model calls (default 4; tuned from the concurrency probe).
- **Rate limiter**: token buckets for RPM and TPM plus a daily request/token counter. It reserves *estimated* tokens before a call and corrects with *actual* usage after. It re-syncs from Groq's `x-ratelimit-remaining-*` headers on every response, because limits can change without notice.
- **Deadlines**: each request carries an absolute deadline; every stage receives the remaining time; the provider call timeout = min(stage cap, remaining).
- **Cancellation**: if the client disconnects, the request task is cancelled, which closes the upstream stream so no more output tokens are spent.
- **Degraded mode** (when TPM headroom is low or the queue is long): fewer passages, no re-rank, no regeneration loop. The answer is still verified — degraded means shorter context, never unverified.
- **Daily budget exhausted**: no model calls. Reply = an explicit message that the model's daily quota is used up, the reset time if known, and the top retrieved passages with their locations, clearly labelled as "relevant passages, not an answer". No generated claims.

Who is affected at each limit (to be confirmed by experiment):
| Situation | Delayed | Dropped | Weaker answer |
|---|---|---|---|
| Near per-minute limit | requests whose estimated wait fits their deadline | requests whose deadline cannot be met (rejected at admission, not after waiting) | new requests while headroom is low (degraded retrieval) |
| Per-day limit gone | nobody (waiting cannot help) | — | everyone: passages-only reply + reset time |

## 4. Retries

- Retry only: 429 (honour `retry-after`), 5xx, connection errors, timeouts *before the first token*.
- Never retry: 4xx other than 429, failures after tokens were already delivered, or when the remaining deadline is shorter than the expected backoff + expected call time.
- Exponential backoff with full jitter, max 2 retries per model call, with a global retry budget (retries <= 20% of calls in a sliding window) to prevent retry storms.

## 5. Model backends — one client, two servers

The stand-in speaks **Groq's OpenAI-compatible wire format**: same endpoint path, same SSE streaming chunks, same `usage` block, same `x-ratelimit-*` and `retry-after` headers, same 429 body shape. The pipeline uses one HTTP provider class and switches backend only by `base_url`. This means the exact code path used in load tests is the one used against Groq.

We call the REST API with `httpx` rather than the `groq` SDK, because the SDK retries automatically by default (which would hide retries from our measurements) and makes raw headers harder to read on streams.

Stand-in behaviour (each knob calibrated from real probes or marked assumed):
- latency = sampled TTFT + output tokens / sampled tokens-per-second, from fitted real distributions
- load factor: latency multiplied by `1 + alpha * in_flight` (alpha assumed — the free tier cannot be loaded enough to measure it)
- RPM / TPM / RPD / TPD limits with Groq-style headers and 429s; configurable (halved-limit profile)
- random failure rate (5xx), slow-success rate (long tail), mid-stream disconnects
- profiles: `normal`, `high_latency`, `flaky`, `rate_limited`, `daily_exhausted`, `heavy_concurrency`, `overload`

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
│   ├── pipeline/               # orchestrator.py, analyzer.py, retrieval.py, decide.py,
│   │                           # generator.py, verifier.py, prompts.py
│   ├── providers/              # base.py, openai_compat.py (Groq and stand-in)
│   ├── infra/                  # admission.py (queue + semaphore), rate_limiter.py,
│   │                           # retry.py, deadline.py, metrics.py
│   ├── simulator/              # server.py, behavior.py, profiles.py
│   └── evaluation/             # questions.py, grading.py, report.py
├── experiments/                # real_probe.py, calibrate.py, load_test.py, retrieval_eval.py
├── data/                       # questions.json, built indexes (generated)
├── papers/                     # the three PDFs + manifest.json (sha256)
├── results/                    # traces, probe logs, run metadata, generated tables
├── scripts/                    # fetch_papers.py
├── tests/
├── docs/
├── results.csv
├── requirements.txt, .env.example, .gitignore, README.md
```

Differences from the suggested layout, and why:
- One `openai_compat.py` provider instead of separate Groq and simulator providers: the stand-in mimics Groq's protocol, so one client exercises the real code path in every test.
- `ingest/` split out of `pipeline/`: indexing happens offline, once per retrieval setting.
- `context_manager.py` + budget checks became `decide.py` (the branch step), because that is where those decisions are made.
- No `request_queue.py` / `concurrency.py` split: one `admission.py` owns queue + semaphore, so queue length and in-flight count are measured in one place.
