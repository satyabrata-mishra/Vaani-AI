# Assignment analysis — The Relay

Source: `The_Relay_Assignment_Brief.pdf` (4 pages). Deadline agreed: **13 Nov 2026**.
Status legend: `[ ]` pending, `[~]` in progress, `[x]` done, `[U]` needs the user (real-world action).

## 1. Requirements checklist

### Functional (F)
| ID | Requirement (brief wording paraphrased) | Where it lands | Status |
|---|---|---|---|
| F1 | Python / C++ / Go — we use Python 3.13 | whole repo | [x] |
| F2 | Pipeline with >= 4 steps | `app/pipeline/orchestrator.py` (6 steps, see ARCHITECTURE.md §2) | [x] |
| F3 | >= 1 tool call | retrieval tool with its own timeout + fault injection, `app/pipeline/retrieval.py` | [x] |
| F4 | >= 1 branch or loop | decide branch (refuse / widen / degraded) + widen-once and regenerate-once loops, `orchestrator.py` | [x] |
| F5 | One step retrieves passages from the library | `app/pipeline/retrieval.py` (BM25 + optional re-rank, 3 settings indexed) | [x] |
| F6 | Library = exactly 1706.03762v7, 2410.00037v1, 2507.07955v2 | `papers/` + `papers/manifest.json` (sha256) | [x] |
| F7 | Every answer cites where in the papers it came from | citation = paper + arXiv id + section + page; uncited sentences are dropped, `verifier.py` | [x] |
| F8 | Say "the papers do not say" instead of guessing | refuse-before-model (re-rank < -8), `NOT_IN_PAPERS` instruction, verifier refusal; quality on real model pending E7 | [~] |
| F9 | Real hosted model on Groq free tier, own key | `app/providers/openai_compat.py` (tested offline), key via `GROQ_API_KEY` | [~] |
| F10 | Learn limits from docs AND from what the API returns at a limit | `experiments/real_probe.py`, R0-R4 logs and 429 bodies in `results/real_probe/` | [x] |
| F11 | Stand-in service: slow, fails, refuses at limit, slower under load, streams | `app/simulator/` (separate process, Groq-compatible wire format, 8 profiles) | [x] |
| F12 | Stand-in calibrated from real measurements | `app/simulator/calibration.py` + `experiments/standin_check.py` | [x] |
| F13 | Implied by brief: "checks the reply before sending it" | `verifier.py` (whole answer checked before release, D5) | [x] |
| F14 | Implied: people give up sooner than expected | server-side cancellation done (`client_gave_up`, tested); client patience in load generator pending | [~] |
| F15 | Implied: some calls succeed slowly instead of failing | stand-in slow-start tail + hangs; per-request deadline and first-token timeout (tested) | [x] |
| F16 | Implied: limits change without notice | limiter adopts a changed TPM limit header and learns from 429s (tested with the halved profile) | [x] |

### Experiments (E)
| ID | Requirement | Backend | Status |
|---|---|---|---|
| E1 | Modest number of real requests: latency spread, limits, behaviour at a limit | real — R0–R4 done (107 attempts, 50K tokens); RPD idle-refill check pending | [~] |
| E2 | Steady load | stand-in run done (`experiments.load_test steady`); real baseline `steady_real` awaits approval | [~] |
| E3 | Sudden burst | stand-in run done | [x] |
| E4 | Sustained overload | stand-in run done (20/min, re-tuned after a too-gentle first run) | [x] |
| E5 | Per-minute limit halved | stand-in run done (switch without notice at 240 s) | [x] |
| E6 | Daily budget exhausted | stand-in run done (we will NOT deliberately exhaust the real quota) | [x] |
| E7 | >= 15 self-written questions, graded | real; grading by the user by hand | [U] |
| E8 | >= 3 retrieval settings (piece size, pieces fetched, re-rank) | real | [U] |
| E9 | Tokens used per answer | tokens_in/out per request in every run; real numbers need R5/R6 | [~] |
| E10 | "A number we could not have guessed without running it" | pick from results | [ ] |
| E11 | One genuinely slow request split into waiting / working / retrying, before vs after first token | trace file (`experiments/slow_request.py`; stand-in example done, real example after R5/R6) | [~] |

### Report (R) — max 2 pages, headings in this order
| # | Heading | Needs | Status |
|---|---|---|---|
| 1 | What I built and why | design + the one decision that shaped it most | [ ] |
| 2 | The real service and my stand-in | limits + how found; stand-in behaviour, calibration, where least like Groq | [ ] |
| 3 | Results | TTFT and total at p50 and slow end (state percentiles and n), failures, given-up, per load shape; real vs stand-in; quality on >= 15 questions; tokens/answer | [ ] |
| 4 | Where the time goes | one slow request, waiting/working/retrying, before/after first token, show evidence | [ ] |
| 5 | When a limit is hit | per-minute and per-day: who is delayed, dropped, weaker answer, why | [ ] |
| 5a | Retrieval: quality versus cost | >= 3 settings; quality, tokens, TTFT; choice + what happens near the limit | [ ] |
| 5b | How this library will mislead a naive retriever | 3 ways, each with an example question | [ ] |
| 6 | Retries | policy + one case where retrying made it worse | [ ] |
| 7 | What the person sees | reply at 3pm when the daily budget is gone | [ ] |
| 8 | What I'm unsure about, what I left out, what I learned | least certain assumption + what breaks; 2 things not built; 1 thing learned | [U] |
| + | Your feedback on Vaani | visit vaani.dev; stood out, confused, one change | [U] |
| last | How I worked with the assistant | real history; 3 prompts; 1 mistake caught; 1 overrule; what is yours (half page) | [U] |

Placement of the Vaani heading is not specified by the brief; we put it after heading 8 and before the final assistant section.

### Tables (T) — copied into the report, recomputable from `results.csv`
| ID | Table | Source of numbers | Status |
|---|---|---|---|
| T1 | Real limits: RPM, TPM, RPD, TPD, in-flight, response at limit, recovery; value + method + confidence | probe logs (`results/real_probe/`) + docs | [ ] |
| T2a | Latency per scenario (>= 5 rows): real/stand-in, requests, duration s, offered rate/min, TTFT p50/p95/p99, total p50/p95/p99 | `results.csv` | [ ] |
| T2b | Outcomes per scenario: success %, rate-limited %, timed out/given up %, retries/request, peak queue, tokens/answer, cost/1k answers | `results.csv` (+ price constant) | [ ] |
| T3 | Retrieval settings (>= 3): questions, correct+cited %, wrong %, correctly refused %, tokens/answer, TTFT p50/p95, total p50/p95 | `results.csv` | [ ] |

Formatting rules: times in ms, percentages one decimal, "not measured" where absent, no invented numbers.

### Deliverables (D)
| ID | Item | Status |
|---|---|---|
| D1 | Code | [ ] |
| D2 | `results.csv` — exactly: `request_id,scenario,source,start_time,first_token_ms,total_ms,status,retries,tokens_in,tokens_out,retrieval_setting,answer_grade` | [ ] |
| D3 | Raw results for real and stand-in runs (traces kept alongside) | [ ] |
| D4 | 2-page report | [ ] |
| D5 | Real, uncleaned conversation history with the assistant | [U] |

### Constraints (C)
- C1 Own free Groq account; key never committed (`.env` is git-ignored).
- C2 No extra accounts or tricks to get around limits.
- C3 The daily limit is shared by everything we run (tests, probes, experiments) — plan every real request.
- C4 Short live interview afterwards where the system will be changed live → keep code small and explainable.
- C5 "Only submit what you can defend."

## 2. Real vs stand-in split

| Needs the real Groq API | Can use the stand-in |
|---|---|
| Latency distribution (TTFT, total) at our prompt sizes | Steady / burst / sustained-overload load shapes |
| Rate-limit headers and 429 body/shape | Per-minute limit halved |
| Per-minute limit behaviour + recovery (`retry-after`) | Daily budget exhausted (we will not burn the real daily quota) |
| Concurrency behaviour (small parallel probe) | Random failures, slow successes, latency growth under load |
| Retrieval-setting evaluation (answer quality requires a real model) | Retry-storm demonstration |
| Answer quality on the 15+ questions | Unit/integration tests (never use real quota) |

## 3. Measurements to collect

Per request (`results.csv`): the 12 required columns.

Per request, sidecar trace (`results/traces.jsonl`, joined on `request_id`):
arrival, queue enter/exit, each stage start/end (analyze, retrieve, decide, generate, verify, deliver),
each provider attempt (start, first byte, first content token, end, status, headers, backoff waited),
degraded flag, queue depth at arrival, in-flight count at arrival, cancellation reason.

Per real probe (`results/real_probe/*.jsonl`): raw response headers, status codes, 429 bodies, `retry-after`, timestamps.

Per experiment run (`results/runs/<run_id>/meta.json`): config, seed, simulator profile, model, git commit, offered rate, duration.

## 4. Dependencies between tasks

```
fetch papers ─► parse+chunk ─► BM25 index ─► (reranker) ─► retrieval tool
                                                   │
question set (user writes/approves) ───────────────┤
                                                   ▼
Groq provider ─► real probes (E1, needs key) ─► calibration ─► stand-in profiles
     │                                                             │
     └──► orchestrator (queue, limiter, retries, deadlines) ◄──────┘
                    │
                    ├──► retrieval eval on real Groq (E7, E8) ─► user grades
                    └──► load scenarios on stand-in (E2–E6)
                                    │
                    results.csv + traces ─► report script ─► tables ─► report
```

Critical path: real probes must happen early, because the stand-in's calibration and the limiter's settings depend on them.

## 5. Facts established so far (with evidence)

- Paper versions verified from the arXiv stamp inside each PDF: `arXiv:1706.03762v7`, `arXiv:2410.00037v1`, `arXiv:2507.07955v2`. Checksums in `papers/manifest.json`.
- Sizes (pypdf, 11 Nov 2026): Attention 15 pages / ~6.1K words (references from p.10); Moshi 66 pages / ~30.4K words (references from p.42); H-Net 39 pages / ~21.9K words (references from p.25). References and appendices are a large share of the text.
- Groq docs (fetched 11 Nov 2026, https://console.groq.com/docs/rate-limits) list free-tier limits for `openai/gpt-oss-20b` as 30 RPM, 1K RPD, 8K TPM, 200K TPD. `llama-3.1-8b-instant` is now listed as Enterprise ("Contact Sales"), so it is not usable. **Documented, not yet measured.**
- Groq headers per docs: `x-ratelimit-limit/remaining/reset-requests` refer to **RPD**; `...-tokens` refer to **TPM**; `retry-after` only on 429. There is no documented header for RPM or TPD.
- Groq list price (https://console.groq.com/docs/models, 11 Nov 2026) for `openai/gpt-oss-20b`: $0.075 / 1M input tokens, $0.30 / 1M output tokens. Used for the cost-per-1,000-answers column, because the free tier itself costs $0.
