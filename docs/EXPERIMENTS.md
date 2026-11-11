# Experiments — plan (to be filled with results)

## Real-API budget plan (needs user approval before each run)

Account limits for `openai/gpt-oss-20b` *(doc, not yet measured)*: 30 RPM, 8K TPM, 1K RPD, 200K TPD.
Everything we run shares these, including any manual testing in the Groq console.

| Run | Purpose | Requests | Est. tokens | When |
|---|---|---|---|---|
| R0 | `smoke`: one tiny call, read headers | 1 | ~100 | Day 1 |
| R1 | `latency`: 3 prompt sizes (~316 / ~1.5K / ~3K input tokens) x 8, round-robin, paced to <= 70% of TPM | 24 | <= ~46K | Day 1 |
| R2 | `rpm`: tiny requests every 0.5 s until three 429s, then a poll every 3 s until recovery | <= 70 | <= ~6K | Day 1 |
| R3 | `tpm`: ~2K-token requests back-to-back until a 429, then a poll every 3 s until recovery | <= 38 | <= ~29K (worst case; rejected polls should cost 0) | Day 1 |
| R4 | `concurrency`: 8 tiny requests at once | 8 | ~1K | Day 1 |

Script: `python -m experiments.real_probe <probe>`; without `--yes` it only prints the plan and the spend recorded so far (`results/groq_spend.jsonl`, written for every real attempt).
| R5 | Retrieval settings: 3 settings x 21 questions, paced by our limiter (`experiments.retrieval_eval`) | 63 (+~10 regenerations) | ~114K (dry-run estimate, upper bound) | needs approval |
| R6 | Real steady baseline `steady_real`: 12 questions at 3/min (`experiments.load_test steady_real --backend real`) | 12 | ~21K (dry-run estimate) | needs approval |

The R5/R6 estimates come from `experiments/budget.py`: it builds the exact prompt each question would send and counts
it, then assumes 300 output tokens per call (observed: 60–230) and one regeneration for 20 % of calls.

Total <= ~82K tokens on day 1 (worst case) and ~150K on day 2: inside 200K TPD per day, with headroom for mistakes.
We will not deliberately exhaust the real daily quota; the daily-exhausted scenario runs on the stand-in, and any real daily 429 we happen to see is recorded as evidence.

## Real measurements log

### R0 smoke — 2026-11-11 ~11:08 IST, 1 request (`results/real_probe/smoke_*.jsonl`)
Region header: `x-groq-region: bom`. Status 200. 78 tokens in, 16 out (7 of them reasoning), `finish_reason=length`.

| Observation | Value | What it tells us |
|---|---|---|
| `x-ratelimit-limit-requests` | 1000 | matches documented RPD for gpt-oss-20b |
| `x-ratelimit-limit-tokens` | 8000 | matches documented TPM |
| `remaining-tokens` after the call | 7906 = 8000 - 94 | TPM is charged in + out tokens (78 + 16). Cannot yet tell whether it reserves `max_completion_tokens` up front, because the output hit the max (16) |
| `reset-tokens` | 705 ms | 94 tokens / (8000 / 60 s) = 0.705 s exactly → the TPM bucket **refills continuously** at ~133 tokens/s; it is not a fixed 1-minute window |
| `reset-requests` | 1m26.4s | 86400 s / 1000 = 86.4 s exactly → the daily request budget also **refills continuously** (one request every 86.4 s), not at midnight. Hypothesis to confirm on later calls |
| Timeline | headers 890 ms, first reasoning 921 ms, first content 937 ms | single cold request (includes TCP + TLS setup); not a latency sample |
| Token accounting | 16 out = 7 reasoning + ~1 content + ~8 unaccounted | possible hidden formatting tokens in gpt-oss output; one sample, to check in R1 |

Predictions that R2 (`rpm`) will test: if RPM is also a continuous bucket (30 per 60 s = 0.5 requests/s refill), sending every 0.5 s should hit the first 429 after ~20 requests and recover within ~2 s; if it is a fixed one-minute window, after ~30 requests with recovery up to 60 s.
(Correction: at 0.5 s spacing a 30/min bucket refills ~7.5 requests during the 15 s burst, so a continuous bucket predicts ~37 accepted, not ~20.)

### R1–R4 — 2026-11-11 05:41–05:57 UTC (`python -m experiments.analyze_probes` → `results/real_probe/summary.json`)
Totals after R0–R4 (ledger): 107 attempts, 50,358 tokens.

**R1 latency** — 8 requests per prompt size, round-robin, sequential, warm connection. Output ~85–93 tokens (two-sentence answer). ms, nearest-rank percentiles; with n = 8, p95 = the maximum.

| Input tokens (actual) | Headers p50 / max | First token p50 / max | Total p50 / max | After first token p50 | Reasoning tokens |
|---|---|---|---|---|---|
| 310 | 531 / 703 | 532 / 796 | 610 / 828 | 47 | 15 |
| 1,258 | 469 / 688 | 578 / 813 | 656 / 875 | 78 | 6 |
| 2,548 | 547 / 3,625 | 719 / 3,828 | 781 / 3,937 | 63 | 6 |

Findings:
- Prompt size matters little: about +90 ms first-token time per extra 1K input tokens (p50 532 → 719 ms from 310 → 2,548 tokens).
- Almost all the time is before the first token. Generating ~90 output tokens takes only 15–156 ms after the first token (~1,000 tokens/s). Headers arrive just before the first reasoning token, so the wait is server-side queueing + prefill, not streaming.
- One of 24 requests (4.2%) was a "slow success": 3,625 ms waiting for headers, then normal generation. The tail is in *waiting to start*, not in generation.
- Reasoning at `low` effort is cheap here: 5–15 tokens.

**R2 per-minute requests** — two independent runs, 3 minutes apart, one request every ~0.5 s.
- Both runs: exactly **30 accepted** (in 15.4–15.5 s), then 429. Message: `... on requests per minute (RPM): Limit 30, Used 30, Requested 1. Please try again in 2s.` `retry-after: 2`.
- A continuous 30/min bucket would have allowed ~37 in 15 s; a 60 s sliding or fixed window would not recover for ~45 s. Observed: a request 3 s after the last 429 (4.5 s after the first) **succeeded** in both runs. So: a burst of 30 is allowed, and recovery is fast; the exact algorithm is not determined, and the sustained rate after a burst was not measured.
- 429s are fast (62–156 ms) and **do not consume the daily request budget** (remaining-requests unchanged across three 429s).

**R3 per-minute tokens** — ~1.7K-token requests back-to-back.
- 4 accepted in 1.9 s (6,904 tokens), 5th rejected: `on tokens per minute (TPM): Limit 8000, Used 6567, Requested 1692. Please try again in 1.9425s.` `retry-after: 2` (the header rounds; the message has the precise value).
- Check: 6,567 + 1,692 - 8,000 = 259 tokens over; 259 / (8,000/60 s) = 1.94 s → the TPM limit is a **continuously refilling bucket** (~133 tokens/s). High confidence.
- "Requested 1692" ≈ the prompt (1,676 by the usage block) + 16, **not** prompt + `max_completion_tokens` (50). So Groq's admission check appears to use the prompt size, not the output reservation; output tokens are charged afterwards.
- Recovered on the first poll, 3.7 s after the 429.

**R4 concurrency** — 8 requests launched within 62 ms: all 200. Headers p50 594 ms (vs ~500 ms sequential). No in-flight limit reached at 8; higher concurrency not tested (it would just hit RPM/TPM). Remaining-count headers came back **out of order** (997, 998, 994, 995, 999, ...), so a single header is a stale snapshot under concurrency.

**Daily request budget (RPD)** — the `x-ratelimit-reset-requests` header always equals *requests used × 86.4 s* exactly (e.g. 98 used → `2h21m7.2s`), which is what a bucket refilling 1 request per 86.4 s would report. But over 18 minutes the remaining count recovered only 2 requests, where continuous refill predicts ~12.5. **The smoke-test hypothesis (continuous daily refill) is not supported.** The reset header should not be trusted as a promise. Planned check: one tiny request after >= 1 hour idle to see how much has come back.

**Daily tokens (TPD)** — no header exposes it. Our own ledger is the only meter: 50,358 tokens used so far against the documented 200,000.

Calibration targets for the stand-in (from the rows above): base first-token time ~ lognormal around 530 ms + ~90 ms per 1K input tokens; ~4% slow-start tail of ~3–4 s before headers; ~1,000 output tokens/s; 429 latency ~60–160 ms with `retry-after` rounded to whole seconds; RPM burst 30; TPM bucket 8,000 refilling continuously; 429s do not consume daily requests. Small samples (n = 24 latency), so the tail rate is uncertain.

### R5 attempt 1 — 2026-11-11 13:31–13:40 UTC, INVALID for Table 3 (kept as raw evidence, not selected)

- 63 real requests, ~67K tokens (`results/groq_spend.jsonl`), all three settings; runs
  `20261111T133213Z_…small_k6_real`, `…T133343Z_…medium_k4_rr_real`, `…T133612Z_…large_k2_rr_real`.
- **What went wrong:** degraded mode was active in the evaluation. The script paced each question until our limiter
  had room for *one* request, but degraded mode switches on below 25 % TPM headroom, so 16/21, 20/21 and 20/21 requests
  used 2 passages instead of the setting's 6/4/2. The settings were therefore not compared.
- **Caught by:** Table 2b's "Degraded %" column (76.2 % for the first setting) while building the report.
- **Second mistake:** stopping the run with `Stop-Process` on the shell's PID killed only the wrapper; the Python child
  finished all three settings (~40K tokens more than if the stop had worked). Real runs must be stopped by the
  Python process's own PID.
- **Fix:** `Pipeline(allow_degraded=False)` for the evaluation, and the script now aborts if any request runs degraded.
  Verified on the stand-in (all 63 requests non-degraded; `small_k6` prompts back to ~1,300 tokens).
- Real-model behaviour seen in these runs (2-passage prompts): 0 errors, 0 429s, answers in ~0.4–3.4 s; several
  answerable questions refused with `NOT_IN_PAPERS` (e.g. M1, H4 under `small_k6`) — expected with 2 passages.
- Day total after this: ~118K of the documented 200K tokens (R0–R4 ~50K + this ~67K).

## Stand-in model service (Phase 4)

`python -m app.simulator.server --profile normal --port 8100`. Same endpoint, SSE chunks, `usage` block, `x-ratelimit-*` headers, `retry-after` and 429 message text as Groq, so the pipeline's provider code is identical for both. Profiles: `normal` (calibrated), `high_latency`, `flaky`, `rate_limited` (per-minute limits halved), `daily_exhausted`, `heavy_concurrency`, `overload`, `unlimited`. Switchable at runtime via `POST /admin/profile`.

### Calibration (`python -m app.simulator.calibration` → `data/standin_calibration.json`)
| Parameter | Value | Source |
|---|---|---|
| First byte | 498 ms + 27 ms per 1K input tokens, lognormal sigma 0.17 | R1, n = 23 (fit) |
| Headers → first token | 29 ms + 59 ms per 1K input tokens | R1, n = 24 (fit) — prefill continues after headers |
| Slow-start tail | 4.2% of requests, +2–4 s before first byte | R1: 1 of 24 (+3.1 s); range assumed |
| Generation | ~1,091 tokens/s; 6 reasoning tokens | R1 |
| Slowdown per extra request in flight | 3.7% | R4 vs R2, n = 8 — low confidence |
| RPM | refill 0.5/s, burst 22 | R2: fits both runs (30 accepted in ~15.4 s) and `retry-after: 2` |
| TPM | 8,000-token bucket, continuous refill; prompt checked at admission, output charged after | R0, R3 |
| RPD / TPD | 1,000 / 200,000, no refill during a run | header / documented |
| 429 latency | 79 ms | R2 + R3, n = 7 |

### Check: stand-in vs real on the same probe shapes (`python -m experiments.standin_check`, no quota)
| Prompt | Real first token p50 (n = 8) | Stand-in first token p50 (n = 40) | RPM burst accepted | TPM burst accepted |
|---|---|---|---|---|
| ~310 tokens | 532 ms | 600 ms | real 30 / stand-in 30 | real 4 / stand-in 4 |
| ~1.3K tokens | 578 ms | 713 ms | | |
| ~2.6K tokens | 719 ms | 837 ms | | |

The check caught two calibration errors, now fixed: (1) a constant headers→first-token gap made short prompts ~100 ms too slow and long prompts too fast — on Groq that gap grows with prompt size; (2) a plain 30-request bucket let 40 requests through where Groq allowed 30. The stand-in still runs ~70–120 ms slow at p50, partly because it counts 7–9% more prompt tokens than Groq (character-based estimate).

### Where the stand-in is least like Groq
1. **Behaviour under load — the biggest gap.** We never saw Groq under heavy load: 8 requests at once was the most we sent. The stand-in's slowdown per in-flight request is from one small sample, and its capacity queue and overload profile are invented. On Groq, slowness also comes from *other customers*, so it is not correlated with our own load; in the stand-in it is.
2. **Failures are assumed.** 0 errors in 107 real attempts (so the true rate is probably < 3%); the 5xx, mid-stream errors and hangs in `flaky`/`overload` are not calibrated.
3. **Answers are not real.** The stand-in quotes the first sentence of each passage and cites it; it never refuses unless it gets no passages. Answer quality is measured only on real Groq.
4. **Daily budget refill** is not modelled (Groq's is unclear); the RPM algorithm is inferred, not documented.

## Load scenarios (stand-in) — `python -m experiments.load_test all`

The complete pipeline (retrieval with re-ranking, decide, gate, retries, verification) runs in-process against the
calibrated stand-in at real speed (`scale` 1.0). Arrivals, question choice and patience come from one seeded RNG
(`--seed`, default 0). Capacity at the calibrated limits is ~4–5 full answers per minute (8,000 TPM / ~1,650 tokens).

| Scenario | Stand-in profile | Offered load | Why |
|---|---|---|---|
| `steady` | `normal` | Poisson, 4/min for 10 min | just below capacity |
| `burst` | `normal` | 2/min for 4 min + 20 at once at t = 60 s | sudden spike |
| `overload` | `normal` | Poisson, 12/min for 5 min | ~3x capacity, sustained |
| `limit_halved` | `normal` → `rate_limited` at t = 240 s | Poisson, 4/min for 8 min | provider halves RPM/TPM without notice; usage carries over (`keep_state`) |
| `daily_exhausted` | `daily_exhausted` (20 requests left today) | Poisson, 6/min for 5 min | daily budget runs out mid-run |

Every request: deadline 30 s, retrieval `medium_k4_rr`. **Patience is assumed**: 20 % of people give up after 8–15 s
(uniform), the rest wait for the system. Giving up cancels the request (status `timeout`, outcome `client_gave_up`).

Per run, `results/runs/<run_id>/` holds `meta.json` (scenario, seed, settings, git commit, peak queue, retry counts,
stand-in state), `results.csv` + `traces.jsonl` (one row/trace per request), `timeline.jsonl` (queue, in-flight and TPM
headroom every second) and `standin_log.json` (every request the stand-in saw). `results/selected_runs.json` names the
run used per scenario; `python -m experiments.runs` rebuilds the root `results.csv` from those runs.

### Load scenario results — first look (stand-in, seed 0, 2026-11-11; `python -m experiments.inspect_run`)

Official Tables 2a/2b are generated by the reporting script (Phase 9). Values below are copied from
`inspect_run` output; first-token percentiles are over `ok` requests, total time over all requests.

| Scenario | Requests | ok | rate_limited | timeout (gave up) | Degraded | First token p50 / p95 (ms) | Peak queue | 429s seen |
|---|---|---|---|---|---|---|---|---|
| steady | 31 | 31 | 0 | 0 | 0 | 2,592 / 5,952 | 1 | 0 |
| burst | 29 | 18 | 11 (turned away at arrival) | 0 | 12 | 2,525 / 28,740 | 5 | 0 |
| overload | 87 | 57 | 16 (turned away at arrival) | 14 | 82 | 16,391 / 27,743 | 5 | 0 |
| limit_halved | 40 | 40 | 0 | 0 | 16 | 2,748 / 7,449 | 1 | 0 |
| daily_exhausted | 23 | 20 | 3 (passages-only reply) | 0 | 0 | 2,724 / 5,573 | 1 | 0 |

Observations (each checked in the traces):
- **No request ever received a 429 from the stand-in in the selected runs.** Our limiter paces calls before sending. In
  `limit_halved` the client read the new 4,000 TPM limit from the first response headers after the switch
  (`learned_tpm_limit` 4000), and degraded mode served the 16 requests after it. This depends on the provider reporting
  its limit in headers, which Groq does (`x-ratelimit-limit-tokens`, R0).
- **Daily exhausted**: the stand-in had 20 requests left; requests 21–23 got the passages-only reply. Our client
  stopped before asking (its count reached the provider's `x-ratelimit-remaining-requests` = 0), so the provider's RPD
  429 was never needed.
- **Overload** (17.4/min realised): degraded mode (2 passages, ~900 tokens per answer instead of ~1,600) roughly doubles
  capacity, but cannot cover 4x. 16 people were told immediately that the wait would exceed their deadline; 14 of the 20
  impatient people (8–15 s) gave up while queued. No patient person hit the 30 s deadline.
- **Burst** (20 at once): 9 answered from the burst, 11 turned away at arrival; queue wait p95 20.9 s. The run reproduced
  the earlier run with the same seed exactly (18 ok / 11 turned away).
- **Re-tuned scenarios.** The first `overload` run (12/min, realised 10/min) was absorbed by degraded mode (47/50 ok,
  0 turned away) and the first `limit_halved` run (Poisson 4/min) realised only ~2.7/min after the switch, so the load
  barely reached the new limit (3,975 tokens/min vs 4,000). Both were re-run with heavier load; the first runs remain in
  `results/runs/` (`…T120225Z_overload…`, `…T120730Z_limit_halved…`).
- **Turned away late.** A request turned away for capacity has already been through retrieval (up to ~1.2 s of
  re-ranking); checking admission before retrieval would save that CPU under overload. Not changed.

## Slow request breakdown (Phase 10) — `python -m experiments.slow_request --source stand-in|real`

Picks the answered request with the largest total time in the selected runs and splits it, from its own trace, into
queue wait, analysis, retrieval, model time before/after the model's first token, failed attempts, retry backoff and
verification (the remainder is shown as "other"). Output: `results/tables/slow_request_<source>.md`.

Stand-in result: request `83c33ab04b00` (overload, U5, degraded), 29,143 ms total: **queue wait 23,483 ms (80.6 %)**,
model 4,151 ms (a simulated slow start: headers after 3,844 ms), re-rank 1,506 ms, verification 1 ms, unexplained 2 ms.
The person saw nothing until 29,143 ms because the answer is verified before release (D5). **Real example: pending**
the clean real runs (R5/R6).

## Daily quota exhausted — what the person sees (Phase 11)

Reply (status `rate_limited`, outcome `daily_budget_exhausted_RPD`), verbatim from the selected `daily_exhausted` run:

> The language model's daily quota at our provider (Groq) is used up (requests per day), so I cannot write an answer
> to your question right now.
> Nothing below is generated: I have not answered from memory or guessed.
> You can ask again after about 12 Nov 14:01 UTC (in ~24.0 h, as reported by the provider; it may refill earlier or later).
> Meanwhile, these are the passages from the papers that best match your question (relevant passages, not an answer):
> [1] … [2] … [3] … (verbatim passage text with paper, section and page)

- The retry time is the provider's own `x-ratelimit-reset-requests` header turned into a clock time; it is stored as an
  absolute time so it does not go stale. With the daily *token* limit (no header) the reply says the reset time is
  unknown. The caveat "may refill earlier or later" is there because we observed no meaningful refill in 18 min (R1–R4).
- Once the limit is known, no further model calls are made (the 2nd and 3rd such replies sent nothing to the provider).
- Tested in `tests/test_pipeline.py` (both the request and the token variant).

## Retrieval settings
Initial values (in `app/config.py`), to be revisited once chunk sizes are measured on the parsed papers:

| Name | Piece size (tokens) | Pieces in prompt | Fetched before re-rank | Re-rank |
|---|---|---|---|---|
| `small_k6` | 200 | 6 | 6 | no |
| `medium_k4_rr` | 350 | 4 | 20 | yes |
| `large_k2_rr` | 700 | 2 | 20 | yes |

All three put roughly 1.2–1.4K passage tokens in the prompt, so the comparison isolates *granularity and re-ranking* at similar cost. A fourth, cheaper setting (for the "limit is close" degraded mode, 2 pieces) is measured as part of the degraded-mode experiment.

Measured after building the index (`data/index/stats.json`): pieces never cross a section boundary, so many pieces are shorter than the target. Median piece: 172 / 312 / 442 tokens; median passage tokens per prompt: 945 / 1,155 / 1,011.

### Retrieval-only evaluation (no model calls) — `python -m experiments.retrieval_offline`, 21 questions
"Hit" = at least one retrieved piece comes from an expected section (or expected page when no section is given).

| Setting | Hits (16 answerable) | Passage tokens p50 | Retrieval time |
|---|---|---|---|
| `small_k6` (BM25 only) | 15 | 945 | ~1 ms |
| `medium_k4_rr` | 16 | 1,155 | ~1.0–1.4 s (re-rank on CPU) |
| `large_k2_rr` | 12 | 1,011 | ~1.3 s |

Findings:
- **Re-ranking costs more time than the model call.** The cross-encoder takes ~1.0–1.4 s per query on this CPU (20 candidates), vs ~1 ms for BM25 and ~600 ms median for the Groq call itself. Under concurrent load it will also compete for CPU. Candidate for "a number we could not have guessed".
- **Retrieval scores cannot decide answerability on their own.** BM25 confidence is 0.26–0.48 for unanswerable questions and 0.18–0.77 for answerable ones. Re-rank score separates only the most off-topic question (U1 "capital of France": -10.4); the paraphrased answerable A5 (-4.9) scores the same as unanswerable U3 (-4.9), and unanswerable U2 scores +7.1. So the pre-model refusal can only catch extreme cases; the model plus verification must handle the rest.
- **The 2-piece large setting loses information**: 4 misses (M3, M4, H4, A5) because the second-best section is not enough.
- **Multi-paper question X1** retrieves only Moshi pieces in the re-ranked settings (the Attention §3.5 half is missing) → needs per-paper retrieval when the question names two papers.

### Evidence for "how this library will mislead a naive retriever" (verified on the parsed text)
1. **Demo transcripts look like general knowledge.** Moshi Appendix F.1 (pp.62–63) contains example dialogues ("the book is set in 17th century France… travels to Paris"). U1 "What is the capital of France?" retrieves only these pieces. A naive pipeline would answer from a demo transcript and cite Moshi.
2. **The right number from the wrong paper.** U2 "What BLEU score does H-Net achieve on WMT 2014 English-to-German?" retrieves the Transformer's BLEU passages with a high re-rank score (+7.1) — H-Net reports no BLEU. A naive answer: "28.4 BLEU", cited to a real passage of the wrong paper.
3. **Tables lose their labels and their section.** The Transformer's results-table row "Transformer (big) 28.4 41.8" is extracted under §5.4 Regularization (the table floats next to it on p.8), with no column headers. A naive answer can cite "§5.4" for a BLEU score, or attach a number to the wrong column.
Also observed: paraphrased questions (A5 "how does it know the order of words") miss "positional encoding" with BM25 alone; and the H-Net abstract says "nearly 4×" while the introduction says "3.6×" for the same DNA result.

## Candidate ways this library misleads a naive retriever (hypotheses — to be verified against the actual text)
1. Shared vocabulary across papers: "attention", "Transformer", "chunk", "tokenizer" appear in all three; questions without a paper name can pull passages from the wrong paper.
2. Reference lists: Moshi (references from p.42) and H-Net (from p.25) cite other papers' titles and claims; a keyword match on a cited title returns a bibliography line instead of content.
3. Tables flattened by PDF extraction: numeric results lose their row/column labels, so a retrieved number can be attached to the wrong model or setting.
