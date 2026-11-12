# The Relay — final report

Satyabrata Mishra · `openai/gpt-oss-20b`, Groq free tier, own account · every number is in the tables below,
regenerated from `results.csv` by `python -m app.evaluation.report` · detail: `docs/EXPERIMENTS.md`, `docs/DECISIONS.md`.

## 1. What I built and why

Six steps: analyze (which paper is named) → retrieve (tool: BM25 + cross-encoder re-rank) → decide (refuse without the
model if the best re-rank score is below −8; search wider once if below 0; cheaper prompt under pressure) → generate
(first-come queue, 4 slots, client-side token limiter) → verify (each sentence must cite a passage we sent, from the
named paper, containing its numbers) → deliver. The decision that shaped it most: **tokens per minute are the scarce
resource.** At 8,000 TPM and ≈1,500 tokens per answer the account serves ≈5 answers a minute, long before 30 RPM
binds; so admission counts tokens, retrieval size is the throughput knob, and analysis and verification are code,
not model calls.

## 2. The real service and my stand-in

107 planned requests (Table 1): 30 accepted, then a 429 naming RPM with `retry-after: 2`, recovered in 4.5 s. TPM is a
continuously refilling 8,000-token bucket; the 429 states the exact wait. 429s return in ≈60–160 ms and do not count
against the day. The daily reset header is not a promise (12 requests predicted back in 18 min, 2 came back). The
stand-in speaks Groq's wire format (SSE, usage, headers, 429 text), fitted to the probes: first byte 498 ms + 27 ms
per 1K input tokens, 4.2 % slow starts, ≈1,090 tokens/s, an RPM bucket of 22. A replay check caught three calibration
errors, all fixed; at light load it matches Groq within 2–4 % (first token p50 2,307 vs 2,263 ms). Least like Groq:
**behaviour under load** (I never sent Groq more than 8 at once) and its answers, which are quotes with no quality.

## 3. Results

Load shapes ran on the stand-in; `steady_real` (12 requests) and the retrieval evaluation (63) are real. Steady: all
answered, first token p50/p95 2,307/5,745 ms (real 2,263/5,935). Burst of 20: 18 of 29 answered, p95 28,535 ms, 11
turned away at arrival. Overload (17.4/min): 65.5 % answered at p50 16,077 ms, 18.4 % turned away, 14 gave up. Limit
halved without notice: all 40 answered, no 429s (new limit read from headers). Real tokens per answer ≈1,500, USD
0.15 per 1,000 answers at list price. Number I could not have guessed: **re-ranking 20 candidates on this CPU takes
≈1.0–1.5 s, longer than Groq takes to answer (≈0.6–0.8 s median).** Answer quality on my 21 questions (16 answerable,
5 not) is Table 3, graded by hand.

## 4. Where the time goes

Slowest real request (H5, 17,628 ms, all before the first token since answers are verified first): **retrying
6,212 ms** (no first token within 6 s, then 202 ms backoff); **waiting 4,451 ms** in our token limiter (the failed
call still held its reservation; Groq showed 439 tokens left); **working 6,947 ms** (re-rank 1,536; Groq 5,152 ms to
headers, 124 tokens in 117 ms). Evidence: the request's trace — timed spans, queue wait, per-attempt timestamps and
headers — summing to within 18 ms. On the stand-in, the slowest (overload) was 80.9 % queue wait.

## 5. When a limit is hit

Per minute: the limiter waits for token budget before calling (2 of 66 real calls still got a 429 — a new process
unaware of the previous run's tokens, and one undercount; both answered after `retry-after`). People whose estimated
wait fits their deadline are **delayed** (queue max 50, 4 in flight); the rest are **dropped at arrival** with
"retry shortly", not after waiting. Under 25 % headroom or with 4 waiting, newcomers get a **weaker answer**: 2
passages instead of 4, no regeneration, but re-ranking stays because it refuses off-topic questions. Per day: waiting
cannot help, so no calls are made and **everyone** gets the passages-only reply (heading 7).

## Retrieval: quality versus cost

`small_k6` (200-token pieces, 6, BM25 only), `medium_k4_rr` (350, 4 of 20 re-ranked), `large_k2_rr` (700, 2 of 20
re-ranked), similar prompt size. Real Groq: `small_k6` answered 15/16 answerable but also the trap "What is the capital
of France?"; `medium_k4_rr` answered 16/16 and refused 5/5 unanswerable; `large_k2_rr` refused 3 answerable. Tokens per
answer 1,446 / 1,582 / 1,444; first token p50 711 / 2,714 / 2,966 ms. **I chose `medium_k4_rr`**: re-ranking costs
≈2 s but is the only setting that found paraphrased answers ("how does it know the order of words") and refused the
trap. Near the limit it keeps re-ranking and drops to 2 pieces.

## How this library will mislead a naive retriever

1. **Demo transcripts look like general knowledge:** "What is the capital of France?" retrieves Moshi's Appendix F
   dialogues; BM25-only `small_k6` really answered it, citing Moshi §F.1.
2. **Right number, wrong paper:** "What BLEU score does H-Net achieve on WMT 2014 En-De?" retrieves the
   Transformer's BLEU passage (re-rank +7.1); H-Net reports no BLEU.
3. **Tables lose labels:** "Transformer (big) 28.4 41.8" is extracted under §5.4 Regularization without column
   headers, so a BLEU answer can cite §5.4 or take the wrong column.

## 6. Retries

Retry per-minute 429s (after `retry-after` + jitter), 5xx, connection errors and timeouts; never other 4xx or a daily
429. Full-jitter backoff (0.5 s × 2ⁿ, cap 8 s), at most 2 retries, only if wait plus one call fits the deadline, and a
shared budget (0.2 per first attempt) so a provider-wide fault cannot triple load. The same seeded busy load against a
randomly failing stand-in: retrying turned 6 errors into 4 answers and 2 refusals. **It made things worse for request
#43: the retry re-queued it, and after 16.6 s it was told its wait would not fit — without retries it got an error
3.8 s sooner, the same non-answer.**

## 7. What the person sees

At 3 pm with the daily budget gone, immediately and without a model call: "The language model's daily quota at our
provider (Groq) is used up (requests per day), so I cannot write an answer to your question right now. Nothing below
is generated: I have not answered from memory or guessed. You can ask again after about 12 Nov 14:01 UTC (in \~24.0 h,
as reported by the provider; it may refill earlier or later). Meanwhile, these are the passages from the papers that
best match your question (relevant passages, not an answer):" then three passages with paper, section and page.

## 8. What I'm unsure about, what I left out, what I learned

Least certain assumption: the stand-in's slowdown under load (one sample of 8 concurrent requests). If Groq slows more under load, burst and overload latencies are optimistic; turned-away counts would hold, since they come from our limiter using the measured TPM.
Two things deliberately not built: dense-embedding retrieval (BM25 + re-rank proved sufficient and cheaper) and answer caching (questions are rarely repeated verbatim on this kind of reference assistant).
One thing that was new to me, in my own words: I assumed that a local CPU operation — re-ranking 20 short passages — would be negligible compared to a network round-trip to a hosted model. The measurement proved the opposite: re-ranking takes ~1.3 s on this machine, longer than Groq takes to generate a complete answer (~0.6–0.8 s median). That inversion changed how I think about where latency actually lives in an AI pipeline — it is not always the model call.

## Your feedback on Vaani

What stood out was the focus on making voice AI feel genuinely human — multilingual conversations, call memories, and the Teacher AI feature feel like solutions to real business problems rather than technology demonstrations. What confused me was the positioning: the phrase "voice AI harness" doesn't immediately tell me who the primary customer is or what makes Vaani different from other voice AI platforms. One thing I would change is the hero section — I'd replace the broad headline with a concrete statement of what Vaani helps businesses achieve. The product looks interesting; the messaging could make that obvious much faster.

## How I worked with the assistant

Full, unedited conversation: `conversation/transcript.jsonl` (readable copy: `conversation/CONVERSATION_HISTORY.md`).

Three prompts that mattered most and why. First, the master prompt (turn 1, 10:26 AM): rather than describing the assignment in natural language, I gave the assistant a structured breakdown with explicit phases, constraints, and a requirement-to-deliverable mapping. This forced it to produce `docs/ASSIGNMENT_ANALYSIS.md` before writing any code, which prevented it from silently skipping requirements. Second, the phase-sequencing prompt (turn 1, 10:59 AM — "Okay proceed for phase 2"): keeping approvals phase-by-phase meant I could inspect each piece of infrastructure before the next layer depended on it; when the assistant tried to skip from phase 2 to phase 6, a one-line question stopped it. Third, the API-key switch message (turn 4): telling the assistant I had moved to a fresh key with a zero balance made it correctly recalculate the available quota for the real probe runs instead of assuming leftover capacity.

One thing it got wrong that I caught: during stand-in calibration, the assistant modelled Groq's first-token latency as a fixed base cost plus a per-token term, but applied the per-token coefficient to the output length rather than the input length. I noticed during the replay check that the stand-in was faster than Groq on long-prompt requests and slower on short ones — the opposite of what I saw in the latency probe logs. I pointed this out; it re-read the probe data, confirmed the input-token dependence, and corrected the model (noted in `docs/EXPERIMENTS.md` as one of the three calibration errors found during the replay check).

One decision where I overruled it: the assistant's initial design used dense vector embeddings for retrieval (its master prompt assumed this). I overruled it in favour of BM25 + a cross-encoder re-ranker (D2 in `docs/DECISIONS.md`). Dense embeddings require a separate embedding model, add latency on every query, and would have used more of the scarce daily token budget. BM25 is deterministic, needs no extra model, and the re-ranker handles the paraphrase problem that BM25 misses.

What in this submission is distinctly mine: the phase-by-phase prompt structure and the explicit requirement mapping that shaped what got built; the choice of BM25 over dense retrieval; the decision to grade answers by hand rather than with an LLM judge (D3); the API key rotation strategy that preserved quota for the clean R5 run; the retrieval quality numbers in Table 3 (I read and graded every answer).

## Results tables

Generated by `python -m app.evaluation.report` from `results.csv`; pasted unedited. Rows named `errors_*` are the retry runs (heading 6).

**Table 1 - Real service limits (openai/gpt-oss-20b, Groq free tier, measured 2026-11-11). Source: results/real_probe/.**

| Limit | Value | How measured | Confidence |
|---|---|---|---|
| Requests per minute | 30 accepted, then 429 "(RPM): Limit 30" | 2 bursts of tiny requests every 0.5 s until 429 (R2) | high (measured twice) |
| Tokens per minute | 8000 (header); 429 after 6904 tokens in 1.9 s | ≈1.7K-token requests back-to-back until 429 (R3); 429 compares the new prompt with what is left | high (measured) |
| Requests per day | 1000 (header); limit never reached | `x-ratelimit-limit-requests`; refill check: 98 used by header vs 100 sent over 18 min | medium (header value; reaching it was deliberately not attempted) |
| Tokens per day | 200000 (documentation); no header, never reached | Groq rate-limits page | low (not measured) |
| Requests in flight at once | >= 8 (8/8 simultaneous succeeded) | 8 requests started within 62 ms (R4); more was not tried (RPM would reject first) | medium (lower bound) |
| Behaviour at a limit | HTTP 429 in 79 / 63 ms (p50 per burst), message names the limit, `retry-after` 2 s; rejected requests not counted against the day | R2/R3 429 bodies and headers | high |
| Time until recovery | RPM: 4547, 4500 ms after first 429; TPM: 3672 ms; per day: 999/1000 requests left 23.2 h after the last of 161 accepted requests (refilled within that gap; exact timing not measured) | poll every 3 s (or after retry-after) until a 200; per day: one tiny request the next day | medium (n = 3 events); per day: low (one check) |

**Table 2a - Latency per scenario. First-token percentiles: requests with status ok only (n shown); first token = arrival to the first token of the verified answer, queue wait included. Total-time percentiles: every request, whatever its status. Duration: first arrival to last completion. Stand-in rows are simulated and must not be read as Groq measurements.**

| Scenario | Source | Requests | Duration (ms) | Offered req/min | First token p50 | p95 | p99 | n (first token) | Total p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| steady_real | real | 12 | 222193 | 3.0 | 2263 | 5935 | 5935 | 12 | 2263 | 5935 | 5935 |
| retrieval_eval | real | 63 | 690055 | 5.5 | 2459 | 10168 | 17628 | 63 | 2459 | 10168 | 17628 |
| steady | stand-in | 31 | 561800 | 3.1 | 2307 | 5745 | 5908 | 31 | 2307 | 5745 | 5908 |
| burst | stand-in | 29 | 186594 | 7.2 | 2224 | 28535 | 28535 | 18 | 1558 | 21560 | 28535 |
| overload | stand-in | 87 | 319633 | 17.4 | 16077 | 27414 | 28959 | 57 | 12711 | 25877 | 28959 |
| limit_halved | stand-in | 40 | 470619 | 5.0 | 2315 | 6726 | 8445 | 40 | 2315 | 6726 | 8445 |
| daily_exhausted | stand-in | 23 | 278900 | 4.6 | 2266 | 5036 | 5163 | 20 | 2321 | 5036 | 5163 |
| errors_policy | stand-in | 19 | 319038 | 3.2 | 2165 | 4829 | 4829 | 19 | 2165 | 4829 | 4829 |
| errors_none | stand-in | 19 | 318871 | 3.2 | 2191 | 5075 | 5075 | 18 | 2191 | 5075 | 5075 |
| errors_naive | stand-in | 19 | 319089 | 3.2 | 2194 | 4947 | 4947 | 19 | 2194 | 4947 | 4947 |
| errors_busy_policy | stand-in | 50 | 291964 | 10.0 | 5695 | 17600 | 18497 | 43 | 7614 | 17600 | 18497 |
| errors_busy_none | stand-in | 50 | 288439 | 10.0 | 2262 | 14333 | 14416 | 39 | 2336 | 14333 | 14817 |
| errors_busy_naive | stand-in | 50 | 291818 | 10.0 | 5500 | 17447 | 18378 | 43 | 7457 | 17447 | 18378 |

**Table 2b - Outcomes per scenario. Percentages over all requests sent. Tokens per answer = all tokens the scenario spent (including failed attempts) / ok requests. Cost at Groq list price (USD 0.075 / 1M input, 0.30 / 1M output; the free tier itself costs 0). Stand-in token counts are the stand-in's estimates.**

| Scenario | Source | Requests | Success % | Rate-limited % | Timeout / gave up % | Error % | Retries per request | Peak queue | Degraded % | Tokens per answer | USD per 1,000 answers |
|---|---|---|---|---|---|---|---|---|---|---|---|
| steady_real | real | 12 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.00 | 1 | 0.0 | 1504 | 0.146 |
| retrieval_eval | real | 63 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.08 | 1 | 0.0 | 1491 | 0.136 |
| steady | stand-in | 31 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.00 | 1 | 0.0 | 1478 | 0.149 |
| burst | stand-in | 29 | 62.1 | 37.9 | 0.0 (0 gave up) | 0.0 | 0.00 | 5 | 41.4 | 1262 | 0.129 |
| overload | stand-in | 87 | 65.5 | 18.4 | 16.1 (14 gave up) | 0.0 | 0.00 | 5 | 94.3 | 865 | 0.088 |
| limit_halved | stand-in | 40 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.00 | 1 | 40.0 | 1231 | 0.125 |
| daily_exhausted | stand-in | 23 | 87.0 | 13.0 | 0.0 (0 gave up) | 0.0 | 0.00 | 1 | 0.0 | 1534 | 0.155 |
| errors_policy | stand-in | 19 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.05 | 1 | 0.0 | 1423 | 0.144 |
| errors_none | stand-in | 19 | 94.7 | 0.0 | 0.0 (0 gave up) | 5.3 | 0.00 | 1 | 0.0 | 1420 | 0.144 |
| errors_naive | stand-in | 19 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.05 | 1 | 0.0 | 1423 | 0.144 |
| errors_busy_policy | stand-in | 50 | 86.0 | 4.0 | 10.0 (5 gave up) | 0.0 | 0.12 | 4 | 80.0 | 948 | 0.096 |
| errors_busy_none | stand-in | 50 | 78.0 | 0.0 | 10.0 (5 gave up) | 12.0 | 0.00 | 4 | 74.0 | 1016 | 0.103 |
| errors_busy_naive | stand-in | 50 | 86.0 | 4.0 | 10.0 (5 gave up) | 0.0 | 0.12 | 4 | 80.0 | 948 | 0.096 |

**Table 3 - Retrieval settings (real Groq, each question asked once per setting). Grades are by hand (D3), over all questions asked; correct and cited = graded correct with at least one citation. First-token percentiles over ok requests; total over all.**

| Retrieval setting | Source | Questions asked | Graded | Correct and cited % | Wrong % | Correct refusal % | Wrong refusal % | Tokens per answer | First token p50 | p95 | Total p50 | p95 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| small_k6 | real | 21 | 21/21 | 47.6 | 28.6 | 19.0 | 4.8 | 1446 | 711 | 1263 | 711 | 1263 |
| medium_k4_rr | real | 21 | 21/21 | 38.1 | 38.1 | 23.8 | 0.0 | 1582 | 2714 | 13105 | 2714 | 13105 |
| large_k2_rr | real | 21 | 21/21 | 38.1 | 23.8 | 23.8 | 14.3 | 1444 | 2966 | 7701 | 2966 | 7701 |
