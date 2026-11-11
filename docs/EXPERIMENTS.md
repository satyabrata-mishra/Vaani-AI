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
| R5 | Retrieval settings: 3 settings x ~20 questions, throttled to stay under TPM | ~60–70 | ~120K | Day 2 (fresh daily window) |
| R6 | Small real steady-load row: ~15 questions at ~3/min | ~15 | ~30K | Day 2, only if budget remains |

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

## Load scenarios (stand-in)
To be designed in Phase 8: steady, burst, sustained overload, per-minute limit halved, daily budget exhausted.

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
