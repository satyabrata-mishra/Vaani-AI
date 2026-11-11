# The Relay — final report

Author: Satyabrata Mishra. Model: `openai/gpt-oss-20b` on the Groq free tier, own account. Code, raw data and
documentation: this repository. Every number below is in the results tables at the end, which `python -m
app.evaluation.report` regenerates from `results.csv` (`--check` confirms they match).

> DRAFT. Items marked **[PENDING: …]** need the clean real runs (R5 retrieval evaluation, R6 real baseline);
> items marked **[YOU WRITE]** must be written by the author. Remove this box before submitting.

## 1. What I built and why

Six steps per question: analyze (which paper is named) → retrieve (tool: BM25 + optional cross-encoder re-rank,
own timeout) → decide (refuse without the model if the best re-rank score is below −8, search wider once if below 0,
use a cheaper prompt under pressure) → generate (one model call through a bounded queue, 4 slots and a client-side
token limiter) → verify (each sentence must cite a passage we sent, from the named paper, and its numbers must appear
there) → deliver.

The decision that shaped it most: **tokens per minute, not requests, are the scarce resource.** At 8,000 TPM and
≈1,500 tokens per answer the account serves ≈5 answers a minute, long before 30 RPM binds. So admission counts tokens,
retrieval size is the throughput knob, and analysis and verification are code, not extra model calls. Measuring also
showed Groq streams ≈1,000 tokens/s, so verifying the whole answer before showing it costs only ≈0.1 s.

## 2. The real service and my stand-in

107 planned requests (Table 1): a burst of 30 is accepted, then a 429 naming RPM with `retry-after: 2`, recovered in
4.5 s. TPM is a continuously refilling 8,000-token bucket: the 429's own numbers give the wait to 0.01 s, and its
admission check counts the prompt, not the output reservation. 429s return in ≈60–160 ms and do not count against
the day; 8 simultaneous requests all succeeded. The daily request header's "reset" is not a promise: it predicted
≈12 requests back in 18 minutes; 2 came back. The 200K tokens/day limit is documented only.

The stand-in speaks Groq's exact wire format (SSE, usage block, rate-limit headers, 429 text), so the pipeline cannot
tell them apart. Fitted to the probes: first byte 498 ms + 27 ms per 1K input tokens (lognormal), 4.2 % slow starts
of +2–4 s, ≈1,090 tokens/s, an RPM bucket of 22 refilling 0.5/s (reproduces "30 accepted in 15.4 s"). Replayed
probes match Groq's bursts exactly and its first-token median to within 68–135 ms. Most unlike Groq: **behaviour
under load** — I never sent Groq more than 8 at once, so the slowdown per in-flight request is one small sample,
failures are assumed (0 in 107 real calls), and answers are extractive quotes with no quality signal.

## 3. Results

Load shapes ran on the stand-in at real speed (Tables 2a/2b; nearest-rank percentiles, first token over answered
requests with n shown, total over all). Steady, 31 requests: all answered, first token p50 2,307 / p95 5,745 ms.
Burst of 20 on a 2/min background: 18 of 29 answered (p95 28,535 ms), 11 turned away at arrival because their wait
could not fit the 30 s deadline. Overload at 17.4/min: 65.5 % answered (p50 16,077 ms), 18.4 % turned away, 14
gave up while queued, 94.3 % ran on the cheaper prompt. Limit halved without notice: all 40 answered, zero 429s —
the client read the new 4,000 TPM from the first response headers. Daily budget exhausted: 3 of 23 got the
passages-only reply. [PENDING: R6 real steady row vs stand-in steady.]

A number I could not have guessed: **re-ranking 20 candidates on this CPU takes ≈1.0–1.4 s, longer than Groq takes
to produce a whole answer (≈0.6–0.8 s median)**; it is ≈1.26 s of the steady run's 2.3 s median first token.

Answer quality on 21 questions I wrote (16 answerable, one needing two papers; 5 not answerable), graded by hand,
and real tokens per answer: [PENDING: R5, Table 3].

## 4. Where the time goes

Slowest answered stand-in request (`63c7cc949372`, overload, 28,959 ms): **waiting 23,432 ms (80.9 %)** in our queue
behind 3 others; working 5,525 ms (re-rank 1,507, model 4,016 — of which 3,859 ms before response headers, a slow
start — and 65 ms streaming); retrying 0. All of it came before the first token, since answers are released only after
verification. Evidence: the request's trace (timed spans per stage, the gate's queue wait, per-attempt timestamps)
and the stand-in's log marking the slow start; the parts sum to within 2 ms. [PENDING: slowest real request.]

## 5. When a limit is hit

Per minute: our limiter waits for token budget, so Groq is never asked into a 429. People whose estimated wait fits
their deadline are **delayed** in a first-come queue (max 50, 4 in flight); the rest are **dropped at arrival** with a
"retry shortly" message, not after waiting. Below 25 % headroom or with 4 waiting, newcomers get a **weaker answer**:
2 passages instead of 4 and no regeneration — but re-ranking stays, because its score is what refuses off-topic
questions. Per day: waiting cannot help, so no one is delayed and no calls are made; **everyone** gets the
passages-only reply (heading 7).

## Retrieval: quality versus cost

Three settings at similar prompt size (≈0.9–1.2K passage tokens): `small_k6` (200-token pieces, 6, BM25 only),
`medium_k4_rr` (350, 4 of 20 re-ranked), `large_k2_rr` (700, 2 of 20 re-ranked). Without the model they found the
expected section for 15, 16 and 12 of 16 answerable questions. [PENDING: R5 quality, tokens, first token per
setting; choice and why.] Near the limit the chosen setting keeps re-ranking and drops to 2 pieces.

## How this library will mislead a naive retriever

1. **Demo transcripts look like general knowledge.** "What is the capital of France?" retrieves only Moshi's
   Appendix F sample dialogues (17th-century France, Paris); a naive system answers and cites Moshi.
2. **The right number from the wrong paper.** "What BLEU score does H-Net achieve on WMT 2014 English-to-German?"
   retrieves the Transformer's BLEU passage with a high re-rank score (+7.1); H-Net reports no BLEU.
3. **Tables lose labels and section.** The row "Transformer (big) 28.4 41.8" is extracted under §5.4
   Regularization without column headers, so an English-to-French BLEU answer can cite §5.4 or take the wrong column.

## 6. Retries

Retry per-minute 429s (after `retry-after` + ≤ 0.25 s jitter), 5xx, connection errors and timeouts; never other 4xx or
a daily-limit 429. Full-jitter backoff (0.5 s × 2ⁿ, cap 8 s), at most 2 retries, only if wait plus one call fits the
deadline, and a shared budget (0.2 retries earned per first attempt) so a provider-wide fault cannot triple the load.
Retries re-enter the queue like new work. I ran one seeded busy load (12/min planned) three times against a stand-in
whose calls fail at random (6 of 48 failed): our policy, no retries, naive retries. Retrying turned 6 errors into 4
answers and 2 refusals at arrival. **It made things worse for request #43: its first call failed, the retry
re-queued it, and after 16.6 s it was told the wait would not fit its deadline — without retries it got an error
after 12.7 s, so 3.8 s more waiting for the same non-answer.** Naive retries behaved identically here (500s carry no
`retry-after`, too few to drain the budget), so this load did not exercise the safeguards; unit tests do.

## 7. What the person sees

At 3 pm, with the daily budget gone, a waiting person receives at once, with no model call:
"The language model's daily quota at our provider (Groq) is used up (requests per day), so I cannot write an answer
to your question right now. Nothing below is generated: I have not answered from memory or guessed. You can ask
again after about 12 Nov 14:01 UTC (in \~24.0 h, as reported by the provider; it may refill earlier or later).
Meanwhile, these are the passages from the papers that best match your question (relevant passages, not an
answer):" then the three best passages with paper, section and page. For the token limit (no header) it says the
reset time is unknown.

## 8. What I'm unsure about, what I left out, what I learned

Least certain assumption [YOU WRITE / confirm]: candidate — the stand-in's slowdown under load (one sample of 8
concurrent requests). If Groq slows more under load, the burst and overload latencies are optimistic; the
turned-away counts would hold, because they come from our own limiter using the measured TPM.
Two things deliberately not built [YOU WRITE / confirm; candidates in DECISIONS.md D9]: dense-embedding retrieval;
answer caching; provider failover.
One thing that was new to me, in my own words: [YOU WRITE]

## Your feedback on Vaani

[YOU WRITE after visiting https://www.vaani.dev/: what stood out, what confused you, one thing you would change.]

## How I worked with the assistant

[YOU WRITE. Attach the real, unedited conversation history export.]
Three prompts that mattered most and why: [YOU WRITE]
One thing it got wrong that I caught, and how: [YOU WRITE — must be something you caught yourself.]
One decision where I overruled it: [YOU WRITE — decisions marked "(User)" in docs/DECISIONS.md are the record.]
What in this submission is distinctly mine: [YOU WRITE]

## Results tables

Generated by `python -m app.evaluation.report` from `results.csv`; pasted unedited. Rows named `errors_*` are the retry runs (heading 6).
[PENDING: re-paste after R5/R6 and grading; Table 3 is empty until then.]

**Table 1 - Real service limits (openai/gpt-oss-20b, Groq free tier, measured 2026-11-11). Source: results/real_probe/.**

| Limit | Value | How measured | Confidence |
|---|---|---|---|
| Requests per minute | 30 accepted, then 429 "(RPM): Limit 30" | 2 bursts of tiny requests every 0.5 s until 429 (R2) | high (measured twice) |
| Tokens per minute | 8000 (header); 429 after 6904 tokens in 1.9 s | ≈1.7K-token requests back-to-back until 429 (R3); 429 compares the new prompt with what is left | high (measured) |
| Requests per day | 1000 (header); limit never reached | `x-ratelimit-limit-requests`; refill check: 98 used by header vs 100 sent over 18 min | medium (header value; reaching it was deliberately not attempted) |
| Tokens per day | 200000 (documentation); no header, never reached | Groq rate-limits page | low (not measured) |
| Requests in flight at once | >= 8 (8/8 simultaneous succeeded) | 8 requests started within 62 ms (R4); more was not tried (RPM would reject first) | medium (lower bound) |
| Behaviour at a limit | HTTP 429 in 79 / 63 ms (p50 per burst), message names the limit, `retry-after` 2 s; rejected requests not counted against the day | R2/R3 429 bodies and headers | high |
| Time until recovery | RPM: 4547, 4500 ms after first 429; TPM: 3672 ms; per day: not measured | poll every 3 s (or after retry-after) until a 200 | medium (n = 3 events) |

**Table 2a - Latency per scenario. First-token percentiles: requests with status ok only (n shown); first token = arrival to the first token of the verified answer, queue wait included. Total-time percentiles: every request, whatever its status. Duration: first arrival to last completion. Stand-in rows are simulated and must not be read as Groq measurements.**

| Scenario | Source | Requests | Duration (ms) | Offered req/min | First token p50 | p95 | p99 | n (first token) | Total p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|---|
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
