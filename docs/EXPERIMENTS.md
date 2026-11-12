# Experiments — method, measurements and results

Everything here is measured unless marked *assumed* or *(doc)*. Real-Groq and stand-in results are kept apart: every
row in `results.csv` has `source` = `real` or `stand-in`, and the stand-in is never quoted as a Groq measurement.

## Real-API budget plan (needs user approval before each run)

Account limits for `openai/gpt-oss-20b` *(doc, not yet measured)*: 30 RPM, 8K TPM, 1K RPD, 200K TPD.
Everything we run shares these, including any manual testing in the Groq console.

| Run | Purpose | Requests | Est. tokens | When |
|---|---|---|---|---|
| R0 | `smoke`: one tiny call, read headers | 1 | ≈100 | Day 1 |
| R1 | `latency`: 3 prompt sizes (≈316 / ≈1.5K / ≈3K input tokens) x 8, round-robin, paced to <= 70% of TPM | 24 | <= ≈46K | Day 1 |
| R2 | `rpm`: tiny requests every 0.5 s until three 429s, then a poll every 3 s until recovery | <= 70 | <= ≈6K | Day 1 |
| R3 | `tpm`: ≈2K-token requests back-to-back until a 429, then a poll every 3 s until recovery | <= 38 | <= ≈29K (worst case; rejected polls should cost 0) | Day 1 |
| R4 | `concurrency`: 8 tiny requests at once | 8 | ≈1K | Day 1 |

Script: `python -m experiments.real_probe <probe>`; without `--yes` it only prints the plan and the spend recorded so far (`results/groq_spend.jsonl`, written for every real attempt).
| R5 | Retrieval settings: 3 settings x 21 questions, paced by our limiter (`experiments.retrieval_eval`) | 63 (+≈10 regenerations) | ≈114K (dry-run estimate, upper bound) | needs approval |
| R6 | Real steady baseline `steady_real`: 12 questions at 3/min (`experiments.load_test steady_real --backend real`) | 12 | ≈21K (dry-run estimate) | needs approval |

The R5/R6 estimates come from `experiments/budget.py`: it builds the exact prompt each question would send and counts
it, then assumes 300 output tokens per call (observed: 60–230) and one regeneration for 20 % of calls.

Total <= ≈82K tokens on day 1 (worst case) and ≈150K on day 2: inside 200K TPD per day, with headroom for mistakes.
We will not deliberately exhaust the real daily quota; the daily-exhausted scenario runs on the stand-in, and any real daily 429 we happen to see is recorded as evidence.

## Real measurements log

### R0 smoke — 2026-11-11 ≈11:08 IST, 1 request (`results/real_probe/smoke_*.jsonl`)
Region header: `x-groq-region: bom`. Status 200. 78 tokens in, 16 out (7 of them reasoning), `finish_reason=length`.

| Observation | Value | What it tells us |
|---|---|---|
| `x-ratelimit-limit-requests` | 1000 | matches documented RPD for gpt-oss-20b |
| `x-ratelimit-limit-tokens` | 8000 | matches documented TPM |
| `remaining-tokens` after the call | 7906 = 8000 - 94 | TPM is charged in + out tokens (78 + 16). Cannot yet tell whether it reserves `max_completion_tokens` up front, because the output hit the max (16) |
| `reset-tokens` | 705 ms | 94 tokens / (8000 / 60 s) = 0.705 s exactly → the TPM bucket **refills continuously** at ≈133 tokens/s; it is not a fixed 1-minute window |
| `reset-requests` | 1m26.4s | 86400 s / 1000 = 86.4 s exactly → the daily request budget also **refills continuously** (one request every 86.4 s), not at midnight. Hypothesis to confirm on later calls |
| Timeline | headers 890 ms, first reasoning 921 ms, first content 937 ms | single cold request (includes TCP + TLS setup); not a latency sample |
| Token accounting | 16 out = 7 reasoning + ≈1 content + ≈8 unaccounted | possible hidden formatting tokens in gpt-oss output; one sample, to check in R1 |

Predictions that R2 (`rpm`) will test: if RPM is also a continuous bucket (30 per 60 s = 0.5 requests/s refill), sending every 0.5 s should hit the first 429 after ≈20 requests and recover within ≈2 s; if it is a fixed one-minute window, after ≈30 requests with recovery up to 60 s.
(Correction: at 0.5 s spacing a 30/min bucket refills ≈7.5 requests during the 15 s burst, so a continuous bucket predicts ≈37 accepted, not ≈20.)

### R1–R4 — 2026-11-11 05:41–05:57 UTC (`python -m experiments.analyze_probes` → `results/real_probe/summary.json`)
Totals after R0–R4 (ledger): 107 attempts, 50,358 tokens.

**R1 latency** — 8 requests per prompt size, round-robin, sequential, warm connection. Output ≈85–93 tokens (two-sentence answer). ms, nearest-rank percentiles; with n = 8, p95 = the maximum.

| Input tokens (actual) | Headers p50 / max | First token p50 / max | Total p50 / max | After first token p50 | Reasoning tokens |
|---|---|---|---|---|---|
| 310 | 531 / 703 | 532 / 796 | 610 / 828 | 47 | 15 |
| 1,258 | 469 / 688 | 578 / 813 | 656 / 875 | 78 | 6 |
| 2,548 | 547 / 3,625 | 719 / 3,828 | 781 / 3,937 | 63 | 6 |

Findings:
- Prompt size matters little: about +90 ms first-token time per extra 1K input tokens (p50 532 → 719 ms from 310 → 2,548 tokens).
- Almost all the time is before the first token. Generating ≈90 output tokens takes only 15–156 ms after the first token (≈1,000 tokens/s). Headers arrive just before the first reasoning token, so the wait is server-side queueing + prefill, not streaming.
- One of 24 requests (4.2%) was a "slow success": 3,625 ms waiting for headers, then normal generation. The tail is in *waiting to start*, not in generation.
- Reasoning at `low` effort is cheap here: 5–15 tokens.

**R2 per-minute requests** — two independent runs, 3 minutes apart, one request every ≈0.5 s.
- Both runs: exactly **30 accepted** (in 15.4–15.5 s), then 429. Message: `... on requests per minute (RPM): Limit 30, Used 30, Requested 1. Please try again in 2s.` `retry-after: 2`.
- A continuous 30/min bucket would have allowed ≈37 in 15 s; a 60 s sliding or fixed window would not recover for ≈45 s. Observed: a request 3 s after the last 429 (4.5 s after the first) **succeeded** in both runs. So: a burst of 30 is allowed, and recovery is fast; the exact algorithm is not determined, and the sustained rate after a burst was not measured.
- 429s are fast (62–156 ms) and **do not consume the daily request budget** (remaining-requests unchanged across three 429s).

**R3 per-minute tokens** — ≈1.7K-token requests back-to-back.
- 4 accepted in 1.9 s (6,904 tokens), 5th rejected: `on tokens per minute (TPM): Limit 8000, Used 6567, Requested 1692. Please try again in 1.9425s.` `retry-after: 2` (the header rounds; the message has the precise value).
- Check: 6,567 + 1,692 - 8,000 = 259 tokens over; 259 / (8,000/60 s) = 1.94 s → the TPM limit is a **continuously refilling bucket** (≈133 tokens/s). High confidence.
- "Requested 1692" ≈ the prompt (1,676 by the usage block) + 16, **not** prompt + `max_completion_tokens` (50). So Groq's admission check appears to use the prompt size, not the output reservation; output tokens are charged afterwards.
- Recovered on the first poll, 3.7 s after the 429.

**R4 concurrency** — 8 requests launched within 62 ms: all 200. Headers p50 594 ms (vs ≈500 ms sequential). No in-flight limit reached at 8; higher concurrency not tested (it would just hit RPM/TPM). Remaining-count headers came back **out of order** (997, 998, 994, 995, 999, ...), so a single header is a stale snapshot under concurrency.

**Daily request budget (RPD)** — the `x-ratelimit-reset-requests` header always equals *requests used × 86.4 s* exactly (e.g. 98 used → `2h21m7.2s`), which is what a bucket refilling 1 request per 86.4 s would report. But over 18 minutes the remaining count recovered only 2 requests, where continuous refill predicts ≈12.5. **The smoke-test hypothesis (continuous daily refill) is not supported.** The reset header should not be trusted as a promise. Planned check: one tiny request after >= 1 hour idle to see how much has come back.

**Daily tokens (TPD)** — no header exposes it. Our own ledger is the only meter: 50,358 tokens used so far against the documented 200,000.

Calibration targets for the stand-in (from the rows above): base first-token time ≈lognormal around 530 ms + ≈90 ms per 1K input tokens; ≈4% slow-start tail of ≈3–4 s before headers; ≈1,000 output tokens/s; 429 latency ≈60–160 ms with `retry-after` rounded to whole seconds; RPM burst 30; TPM bucket 8,000 refilling continuously; 429s do not consume daily requests. Small samples (n = 24 latency), so the tail rate is uncertain.

### R5 attempt 1 — 2026-11-11 13:31–13:40 UTC, INVALID for Table 3 (kept as raw evidence, not selected)

- 63 real requests, ≈67K tokens (`results/groq_spend.jsonl`), all three settings; runs
  `20261111T133213Z_…small_k6_real`, `…T133343Z_…medium_k4_rr_real`, `…T133612Z_…large_k2_rr_real`.
- **What went wrong:** degraded mode was active in the evaluation. The script paced each question until our limiter
  had room for *one* request, but degraded mode switches on below 25 % TPM headroom, so 16/21, 20/21 and 20/21 requests
  used 2 passages instead of the setting's 6/4/2. The settings were therefore not compared.
- **Caught by:** Table 2b's "Degraded %" column (76.2 % for the first setting) while building the report.
- **Second mistake:** stopping the run with `Stop-Process` on the shell's PID killed only the wrapper; the Python child
  finished all three settings (≈40K tokens more than if the stop had worked). Real runs must be stopped by the
  Python process's own PID.
- **Fix:** `Pipeline(allow_degraded=False)` for the evaluation, and the script now aborts if any request runs degraded.
  Verified on the stand-in (all 63 requests non-degraded; `small_k6` prompts back to ≈1,300 tokens).
- Real-model behaviour seen in these runs (2-passage prompts): 0 errors, 0 429s, answers in ≈0.4–3.4 s; several
  answerable questions refused with `NOT_IN_PAPERS` (e.g. M1, H4 under `small_k6`) — expected with 2 passages.
- Day total after this: ≈118K of the documented 200K tokens (R0–R4 ≈50K + this ≈67K).

## Stand-in model service (Phase 4)

`python -m app.simulator.server --profile normal --port 8100`. Same endpoint, SSE chunks, `usage` block, `x-ratelimit-*` headers, `retry-after` and 429 message text as Groq, so the pipeline's provider code is identical for both. Profiles: `normal` (calibrated), `high_latency`, `flaky`, `rate_limited` (per-minute limits halved), `daily_exhausted`, `heavy_concurrency`, `overload`, `unlimited`. Switchable at runtime via `POST /admin/profile`.

### Calibration (`python -m app.simulator.calibration` → `data/standin_calibration.json`)
| Parameter | Value | Source |
|---|---|---|
| First byte | 498 ms + 27 ms per 1K input tokens, lognormal sigma 0.17 | R1, n = 23 (fit) |
| Headers → first token | 29 ms + 59 ms per 1K input tokens | R1, n = 24 (fit) — prefill continues after headers |
| Slow-start tail | 4.2% of requests, +2–4 s before first byte | R1: 1 of 24 (+3.1 s); range assumed |
| Generation | ≈1,091 tokens/s; 6 reasoning tokens | R1 |
| Slowdown per extra request in flight | 3.7% | R4 vs R2, n = 8 — low confidence |
| RPM | refill 0.5/s, burst 22 | R2: fits both runs (30 accepted in ≈15.4 s) and `retry-after: 2` |
| TPM | 8,000-token bucket, continuous refill; prompt checked at admission, output charged after | R0, R3 |
| RPD / TPD | 1,000 / 200,000, no refill during a run | header / documented |
| 429 latency | 79 ms | R2 + R3, n = 7 |

### Check: stand-in vs real on the same probe shapes (`python -m experiments.standin_check`, no quota)
| Prompt | Real first token p50 (n = 8) | Stand-in first token p50 (n = 40) | RPM burst accepted | TPM burst accepted |
|---|---|---|---|---|
| ≈310 tokens | 532 ms | 600 ms | real 30 / stand-in 30 | real 4 / stand-in 4 |
| ≈1.3K tokens | 578 ms | 713 ms | | |
| ≈2.6K tokens | 719 ms | 837 ms | | |

The check caught two calibration errors, now fixed: (1) a constant headers→first-token gap made short prompts ≈100 ms too slow and long prompts too fast — on Groq that gap grows with prompt size; (2) a plain 30-request bucket let 40 requests through where Groq allowed 30. The stand-in still runs ≈70–135 ms slow at p50, partly because it counts 7–9% more prompt tokens than Groq (character-based estimate).

**Third calibration error, found later (Phase 13).** The check above compares first-token times only. While writing
the report, the traces of the stand-in `steady` run showed the model call taking 1,253 ms median, although its first
token (710 ms) matched Groq: streaming 182 tokens after the first one took 604 ms (≈300 tokens/s) instead of
≈170 ms at the calibrated ≈1,090 tokens/s (real Groq: 47–78 ms median for ≈90 tokens, R1). Cause: the stand-in slept
≈1 ms per chunk and Windows rounds every sleep up to its ≈15 ms timer tick. Fix: the stream now sleeps until an
absolute schedule, so overshoot is not accumulated; `tests/test_simulator.py::test_streams_at_the_calibrated_rate_in_real_time`
fails on the old code and passes on the new. Every stand-in load run up to `20261111T145953Z_errors_policy_stand-in` carries the extra
≈0.4–0.5 s per call (and longer slot occupancy under load); those runs are kept in `results/runs/` but no longer
selected, and all five scenarios were re-run.

### Where the stand-in is least like Groq
1. **Behaviour under load — the biggest gap.** We never saw Groq under heavy load: 8 requests at once was the most we sent. The stand-in's slowdown per in-flight request is from one small sample, and its capacity queue and overload profile are invented. On Groq, slowness also comes from *other customers*, so it is not correlated with our own load; in the stand-in it is.
2. **Failures are assumed.** 0 errors in 107 real attempts (so the true rate is probably < 3%); the 5xx, mid-stream errors and hangs in `flaky`/`overload` are not calibrated.
3. **Answers are not real.** The stand-in quotes the first sentence of each passage and cites it; it never refuses unless it gets no passages. Answer quality is measured only on real Groq.
4. **Daily budget refill** is not modelled (Groq's is unclear); the RPM algorithm is inferred, not documented.

## Load scenarios (stand-in) — `python -m experiments.load_test all`

The complete pipeline (retrieval with re-ranking, decide, gate, retries, verification) runs in-process against the
calibrated stand-in at real speed (`scale` 1.0). Arrivals, question choice and patience come from one seeded RNG
(`--seed`, default 0). Capacity at the calibrated limits is ≈4–5 full answers per minute (8,000 TPM / ≈1,650 tokens).

| Scenario | Stand-in profile | Offered load | Why |
|---|---|---|---|
| `steady` | `normal` | Poisson, 4/min for 10 min | just below capacity |
| `burst` | `normal` | 2/min for 4 min + 20 at once at t = 60 s | sudden spike |
| `overload` | `normal` | Poisson, 20/min for 5 min | ≈4x full-answer capacity, ≈2.5x degraded capacity, sustained |
| `limit_halved` | `normal` → `rate_limited` at t = 240 s | constant 5/min for 8 min | just above capacity before the switch, ≈1.9x the halved capacity after; provider halves RPM/TPM without notice; usage carries over (`keep_state`) |
| `daily_exhausted` | `daily_exhausted` (20 requests left today) | Poisson, 6/min for 5 min | daily budget runs out mid-run |

Every request: deadline 30 s, retrieval `medium_k4_rr`. **Patience is assumed**: 20 % of people give up after 8–15 s
(uniform), the rest wait for the system. Giving up cancels the request (status `timeout`, outcome `client_gave_up`).

Per run, `results/runs/<run_id>/` holds `meta.json` (scenario, seed, settings, git commit, peak queue, retry counts,
stand-in state), `results.csv` + `traces.jsonl` (one row/trace per request), `timeline.jsonl` (queue, in-flight and TPM
headroom every second) and `standin_log.json` (every request the stand-in saw). `results/selected_runs.json` names the
run used per scenario; `python -m experiments.runs` rebuilds the root `results.csv` from those runs.

### Load scenario results (stand-in, seed 0, re-run 2026-11-11 15:03–15:36 UTC after the streaming fix)

Official Tables 2a/2b are generated by `app/evaluation/report.py` (`results/tables/`). Values below come from those
tables and the run traces; first-token percentiles are over `ok` requests, total time over all requests.

| Scenario | Requests | ok | rate_limited | timeout (gave up) | Degraded | First token p50 / p95 (ms) | Peak queue | 429s seen |
|---|---|---|---|---|---|---|---|---|
| steady | 31 | 31 | 0 | 0 | 0 | 2,307 / 5,745 | 1 | 0 |
| burst | 29 | 18 | 11 (turned away at arrival) | 0 | 12 | 2,224 / 28,535 | 5 | 0 |
| overload | 87 | 57 | 16 (turned away at arrival) | 14 | 82 | 16,077 / 27,414 | 5 | 0 |
| limit_halved | 40 | 40 | 0 | 0 | 16 | 2,315 / 6,726 | 1 | 0 |
| daily_exhausted | 23 | 20 | 3 (passages-only reply) | 0 | 0 | 2,266 / 5,036 | 1 | 0 |

Compared with the runs before the fix, outcome counts are identical (they are set by our token limiter, which does not
depend on stand-in speed) and first-token medians fell by 0.3–0.6 s.
In the `steady` run the median request spent ≈1.26 s retrieving (mostly re-ranking) and the rest in the model call.

Observations (each checked in the traces):
- **No request ever received a 429 from the stand-in in the selected runs.** Our limiter paces calls before sending. In
  `limit_halved` the client read the new 4,000 TPM limit from the first response headers after the switch
  (`learned_tpm_limit` 4000), and degraded mode served the 16 requests after it. This depends on the provider reporting
  its limit in headers, which Groq does (`x-ratelimit-limit-tokens`, R0).
- **Daily exhausted**: the stand-in had 20 requests left; requests 21–23 got the passages-only reply. Our client
  stopped before asking (its count reached the provider's `x-ratelimit-remaining-requests` = 0), so the provider's RPD
  429 was never needed.
- **Overload** (17.4/min realised): degraded mode (2 passages, ≈900 tokens per answer instead of ≈1,600) roughly doubles
  capacity, but cannot cover 4x. 16 people were told immediately that the wait would exceed their deadline; 14 of the 20
  impatient people (8–15 s) gave up while queued. No patient person hit the 30 s deadline.
- **Burst** (20 at once): 9 answered from the burst, 11 turned away at arrival; queue wait p95 20.9 s. The run reproduced
  the earlier run with the same seed exactly (18 ok / 11 turned away).
- **Re-tuned scenarios.** The first `overload` run (12/min, realised 10/min) was absorbed by degraded mode (47/50 ok,
  0 turned away) and the first `limit_halved` run (Poisson 4/min) realised only ≈2.7/min after the switch, so the load
  barely reached the new limit (3,975 tokens/min vs 4,000). Both were re-run with heavier load; the first runs remain in
  `results/runs/` (`…T120225Z_overload…`, `…T120730Z_limit_halved…`).
- **Turned away late.** A request turned away for capacity has already been through retrieval (up to ≈1.2 s of
  re-ranking); checking admission before retrieval would save that CPU under overload. Not changed.

## Slow request breakdown (Phase 10) — `python -m experiments.slow_request --source stand-in|real`

Picks the answered request with the largest total time in the selected runs and splits it, from its own trace, into
queue wait, analysis, retrieval, model time before/after the model's first token, failed attempts, retry backoff and
verification (the remainder is shown as "other"). Output: `results/tables/slow_request_<source>.md`.

Stand-in result: request `63c7cc949372` (overload, U5, degraded), 28,959 ms total: **queue wait 23,432 ms (80.9 %)**,
model 4,016 ms (a simulated slow start: headers after 3,859 ms; 65 ms from first token to end), re-rank 1,507 ms,
verification 2 ms, unexplained 2 ms. The person saw nothing until 28,959 ms because the answer is verified before
release (D5). (Before the streaming fix the same request was `83c33ab04b00`, 29,143 ms, 80.6 % queue wait.)
Real result: request `49eb48847d50` (R5, `medium_k4_rr`, H5), 17,628 ms total. Its first call got nothing from Groq
within the 6 s first-token timeout (**failed attempt 6,010 ms, 34.1 %**); after 202 ms of backoff the retry **waited
4,451 ms (25.2 %) in our token limiter** (the failed call's token reservation still counted, and Groq's header showed
439 of 8,000 tokens left); then Groq took 5,152 ms to send response headers and 141 ms more to stream 124 tokens.
Re-ranking 1,536 ms, verification 1 ms, other 18 ms. Nothing was queued ahead of it: the slowest real request was
slow because of a provider stall plus our own budget, not because of other people.

## Retries: when does retrying help or hurt? (stand-in, `python -m experiments.load_test retries`)

Same seeded arrivals three times, against the `erroring` profile (configured 20 % of calls fail with a 500 after the
first-byte delay; measured 16.8 % over 400 direct calls). Only the retry behaviour differs: our policy, no retries,
naive (immediate, up to 3, ignores budget and deadline). Runs with the same seed draw the same failures.

| Load | Retry mode | Requests | Model calls | Failed calls | Answered (ok) | Error | Turned away | Gave up | First token p50 / p95 (ms) |
|---|---|---|---|---|---|---|---|---|---|
| 4/min planned (3.2 realised), peak queue 1 | policy | 19 | 19 | 1 | 19 | 0 | 0 | 0 | 2,165 / 4,829 |
| | none | 19 | 18 | 1 | 18 | 1 | 0 | 0 | 2,191 / 5,075 |
| | naive | 19 | 19 | 1 | 19 | 0 | 0 | 0 | 2,194 / 4,947 |
| 12/min planned (10 realised), peak queue 4 | policy | 50 | 48 | 6 | 43 | 0 | 2 | 5 | see Table 2a |
| | none | 50 | 45 | 6 (+1 cut off by a person giving up) | 39 | 6 | 0 | 5 | see Table 2a |
| | naive | 50 | 48 | 6 | 43 | 0 | 2 | 5 | see Table 2a |

Findings:
- **With spare capacity, retrying only helped**: the one failed call (A3) was answered after 3.4 s instead of an error
  after 2.0 s, at no visible cost to anyone else.
- **Busy, retrying turned 6 errors into 4 answers and 2 refusals at arrival.** The 2 were people whose own call failed
  and whose retry, re-entering the queue, could no longer fit their deadline. **Retrying made it worse for one of them
  (request #43, A2): it waited 16.6 s to be told the wait was too long, where without retries the same request ended
  in an error after 12.7 s** — 3.8 s longer for the same non-answer.
- **Our policy and naive retries behaved identically here.** The failures were 500s with no `retry-after`, there were
  only 6 of them (the retry budget of 10 never ran out) and every retry was granted; the 2 refusals came from the
  queue's arrival check on the retry, not from the retry policy. The cases where naive
  retrying is known to hurt (ignoring `retry-after` on 429s; retrying when the deadline cannot be met) did not arise,
  because our limiter sends no request into a known 429. This experiment therefore does not show the policy's
  safeguards working; the unit tests do (`tests/test_infra.py`).
- The 4/min set was too gentle to test anything (peak queue 1) and is kept as the "spare capacity" case.

## Daily quota exhausted — what the person sees (Phase 11)

Reply (status `rate_limited`, outcome `daily_budget_exhausted_RPD`), verbatim from the `daily_exhausted` run
`20261111T135705Z` (the re-run `20261111T153103Z` gives the same text with "12 Nov 15:35 UTC"):

> The language model's daily quota at our provider (Groq) is used up (requests per day), so I cannot write an answer
> to your question right now.
> Nothing below is generated: I have not answered from memory or guessed.
> You can ask again after about 12 Nov 14:01 UTC (in \~24.0 h, as reported by the provider; it may refill earlier or later).
> Meanwhile, these are the passages from the papers that best match your question (relevant passages, not an answer):
> [1] … [2] … [3] … (verbatim passage text with paper, section and page)

- The retry time is the provider's own `x-ratelimit-reset-requests` header turned into a clock time; it is stored as an
  absolute time so it does not go stale. With the daily *token* limit (no header) the reply says the reset time is
  unknown. The caveat "may refill earlier or later" is there because we observed no meaningful refill in 18 min (R1–R4).
- Once the limit is known, no further model calls are made (the 2nd and 3rd such replies sent nothing to the provider).
- Tested in `tests/test_pipeline.py` (both the request and the token variant).

## Retrieval settings
Values in `app/config.py`:

| Name | Piece size (tokens) | Pieces in prompt | Fetched before re-rank | Re-rank |
|---|---|---|---|---|
| `small_k6` | 200 | 6 | 6 | no |
| `medium_k4_rr` | 350 | 4 | 20 | yes |
| `large_k2_rr` | 700 | 2 | 20 | yes |

All three put roughly 1.2–1.4K passage tokens in the prompt, so the comparison isolates *granularity and re-ranking* at similar cost. When the limit is close, degraded mode keeps the chosen setting's re-ranking but sends 2 pieces (D13); its cost shows in the load runs (Table 2b "Degraded %" and tokens per answer), not in Table 3.

Measured after building the index (`data/index/stats.json`): pieces never cross a section boundary, so many pieces are shorter than the target. Median piece: 172 / 312 / 442 tokens; median passage tokens per prompt: 945 / 1,155 / 1,011.

### Retrieval-only evaluation (no model calls) — `python -m experiments.retrieval_offline`, 21 questions
"Hit" = at least one retrieved piece comes from an expected section (or expected page when no section is given).

| Setting | Hits (16 answerable) | Passage tokens p50 | Retrieval time |
|---|---|---|---|
| `small_k6` (BM25 only) | 15 | 945 | ≈1 ms |
| `medium_k4_rr` | 16 | 1,155 | ≈1.0–1.4 s (re-rank on CPU) |
| `large_k2_rr` | 12 | 1,011 | ≈1.3 s |

Findings:
- **Re-ranking costs more time than the model call.** The cross-encoder takes ≈1.0–1.4 s per query on this CPU (20 candidates), vs ≈1 ms for BM25 and ≈600 ms median for the Groq call itself. Under concurrent load it will also compete for CPU. Candidate for "a number we could not have guessed".
- **Retrieval scores cannot decide answerability on their own.** BM25 confidence is 0.26–0.48 for unanswerable questions and 0.18–0.77 for answerable ones. Re-rank score separates only the most off-topic question (U1 "capital of France": -10.4); the paraphrased answerable A5 (-4.9) scores the same as unanswerable U3 (-4.9), and unanswerable U2 scores +7.1. So the pre-model refusal can only catch extreme cases; the model plus verification must handle the rest.
- **The 2-piece large setting loses information**: 4 misses (M3, M4, H4, A5) because the second-best section is not enough.
- **Multi-paper question X1** retrieves only Moshi pieces in the re-ranked settings (the Attention §3.5 half is missing) → needs per-paper retrieval when the question names two papers.

### Evidence for "how this library will mislead a naive retriever" (verified on the parsed text)
1. **Demo transcripts look like general knowledge.** Moshi Appendix F.1 (pp.62–63) contains example dialogues ("the book is set in 17th century France… travels to Paris"). U1 "What is the capital of France?" retrieves only these pieces. A naive pipeline would answer from a demo transcript and cite Moshi.
2. **The right number from the wrong paper.** U2 "What BLEU score does H-Net achieve on WMT 2014 English-to-German?" retrieves the Transformer's BLEU passages with a high re-rank score (+7.1) — H-Net reports no BLEU. A naive answer: "28.4 BLEU", cited to a real passage of the wrong paper.
3. **Tables lose their labels and their section.** The Transformer's results-table row "Transformer (big) 28.4 41.8" is extracted under §5.4 Regularization (the table floats next to it on p.8), with no column headers. A naive answer can cite "§5.4" for a BLEU score, or attach a number to the wrong column.
Also observed: paraphrased questions (A5 "how does it know the order of words") miss "positional encoding" with BM25 alone; and the H-Net abstract says "nearly 4×" while the introduction says "3.6×" for the same DNA result.
Reference lists are excluded from the index (Moshi's from p.42, H-Net's from p.25), so bibliography lines never reach a prompt.

### Real retrieval evaluation (R5) and real baseline (R6) — 12 Nov 2026, own account
- Commands: `python -m experiments.retrieval_eval --yes` (3 settings × 21 questions, degraded mode off; 12:54–13:06
  UTC), then `python -m experiments.load_test steady_real --backend real --yes` (13:06–13:10 UTC).
- The grading sheets of the invalid attempt 1 are in `results/grading/invalid_r5_attempt1/` and must not be graded.
- Grading: `python -m experiments.pregrade` fills only the grades the rubric fixes mechanically (a refusal of an
  answerable question = `refused_wrongly`; of an unanswerable one = `refused_correctly`; an answer to an unanswerable
  one = `wrong`) and marks them `[rule: …]` in `grader_note`. Answers to answerable questions are graded by hand
  (D3), then `python -m experiments.apply_grades` and `python -m app.evaluation.report`.

**R5 — what each setting did** (all 63 requests status ok, none degraded, no daily limit):

| Setting | Answerable answered (of 16) | Answerable refused | Unanswerable refused (of 5) | Tokens per answer | First token p50 / p95 (ms) |
|---|---|---|---|---|---|
| `small_k6` (BM25 only) | 15 | A5 | 4 — **answered U1** | 1,446 | 711 / 1,263 |
| `medium_k4_rr` | 16 | — | 5 | 1,582 | 2,714 / 13,105 |
| `large_k2_rr` | 13 | A2 (failed verification), A5, M3 | 5 | 1,444 | 2,966 / 7,701 |

- **The naive-retriever trap fired for real.** `small_k6` answered U1 "What is the capital of France?" from Moshi's
  Appendix F.1 demo dialogue and cited it (§F.1, p.63). Both re-ranked settings refused U1 before calling the model
  (re-rank score below −8).
- **Paraphrase needs re-ranking.** A5 ("how does it know the order of words") was refused by BM25-only `small_k6`
  and by 2-piece `large_k2_rr`; only `medium_k4_rr` found §3.5 Positional Encoding.
- **Two pieces are too few.** `large_k2_rr` refused three answerable questions; A5 and M3 were also offline misses.
- **Cost of re-ranking is time, not tokens:** tokens per answer differ by under 10 %; first-token p50 is ≈2 s higher
  with re-ranking, of which ≈1.3–1.5 s is CPU re-ranking.
- **Chosen setting: `medium_k4_rr`** (already the default in `app/config.py`): the only setting that answered all 16
  answerable questions and refused all 5 unanswerable ones. Correctness of the answers themselves is Table 3, by hand.
- **Real 429s.** 2 of 66 real calls got a per-minute token 429; both were retried after Groq's `retry-after` and
  answered. A1 was the first request of the `medium_k4_rr` run, which started seconds after the `small_k6` run in a new
  process whose limiter did not know the previous run's tokens; for M1, Groq counted 5,806 tokens used, more than our
  estimate. 3 calls timed out before their first token (6 s) and were answered on retry.

**R6 — real steady baseline vs stand-in** (`steady_real`: 3/min at constant spacing for 4 min, nobody impatient;
`steady`: Poisson ≈4/min planned for 10 min; both draw from the same 21 questions):

| | Requests | Answered | First token p50 / p95 (ms) | Tokens per answer | 429s |
|---|---|---|---|---|---|
| Real Groq (`steady_real`, 12 requests, 3/min) | 12 | 12 | 2,263 / 5,935 | 1,504 | 0 |
| Stand-in (`steady`, 31 requests, 3.1/min) | 31 | 31 | 2,307 / 5,745 | 1,478 | 0 |

At light load the stand-in's first-token p50, p95 and tokens per answer are within 2–4 % of the real service. With
12 real requests the nearest-rank p95 is the slowest request, so this is a rough match only; it says nothing about
behaviour under load, which was not tested on Groq.
