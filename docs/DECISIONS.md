# Decisions log

Each entry: decision, alternatives, trade-off, who decided. "User" = Satyabrata Mishra; "Assistant" = proposed by the AI assistant and not yet challenged.

## D1. Primary model: `openai/gpt-oss-20b` (User chose "small/fast"; Assistant mapped it to this model)
- The user chose the small, fast model. `llama-3.1-8b-instant` is now Enterprise-only on Groq (models page, 11 Nov 2026), so the smallest free-tier chat model is `openai/gpt-oss-20b`.
- Alternatives: `openai/gpt-oss-120b` (same documented limits, slower, better answers); `qwen/qwen3.8-27b` (preview model, can be withdrawn).
- Trade-off: gpt-oss is a reasoning model. Reasoning tokens delay the first answer token and count as output tokens. We set the lowest reasoning effort and measure it.
- Open: whether to use 120B as an "upgrade" path. Not planned — same limits, so it buys nothing under load.

## D2. Retrieval: BM25 + optional cross-encoder re-rank (User)
- The master prompt says "vector retrieval"; the user's earlier explicit choice was BM25 + cross-encoder. We follow the explicit choice. BM25 is a sparse vector-space model; dense embeddings are left out (see D9).
- Trade-off: BM25 misses paraphrases ("how does the model know token order?" vs "positional encoding"). The re-ranker and the analyzer's alias table partly offset this. To be measured in the retrieval experiment.

## D3. Grading: by hand against a rubric (User)
- No LLM judge: it would consume the same scarce daily tokens and is not proof of correctness.
- The code does the mechanical checks (citation present, cited passage was retrieved, numbers appear in the passage). The user assigns the final `answer_grade`.

## D4. `results.csv` has exactly the 12 required columns; extra data goes in `results/traces.jsonl` (User)
- Peak queue length is computed from the CSV (requests in the system at each moment, from `start_time` + `total_ms`, minus the configured concurrency cap) and cross-checked against the trace.
- The waiting/working/retrying breakdown (report section 4) comes from the trace, not a table.
- `answer_grade` is empty for requests that were never graded (failed requests, stand-in load runs). This is stated in the report.

## D5. Verify the whole answer, then stream it to the user (User, 11 Nov, after R1)
- Originally proposed (Assistant): release each sentence once its citation verifies.
- Measurement that changed it: on real Groq, generating ~90 output tokens takes 15–156 ms after the first token (~1,000 tokens/s); almost all latency is before the first token. Sentence-level release would save ~0.1 s for noticeably more complexity.
- Decision: the model's stream is consumed in full by the server, the whole answer is verified, then the verified answer is streamed to the user.
- `first_token_ms` in `results.csv` = request arrival → first token of the *verified* answer delivered to the user (includes queue wait and verification). The provider's own first-token time is in the trace.
- Cost: when the model is slow (stand-in under overload, long answers), the user's first token waits for the whole generation. That gap is measured, not assumed.

## D6. One OpenAI-compatible HTTP provider for both Groq and the stand-in (Assistant)
- The stand-in imitates Groq's endpoint, SSE format, usage block, headers and 429s.
- `httpx` instead of the `groq` SDK, because the SDK retries automatically by default and that would hide retries from our measurements.

## D7. Experiments drive the pipeline in-process (Assistant)
- The load generator calls the orchestrator directly with asyncio tasks. Model calls still go over HTTP (to Groq or the stand-in process).
- Alternative: drive the FastAPI app over HTTP. More realistic client side, but adds a third process and noise that is not part of the question. The FastAPI app still exists for demos.
- A user giving up is simulated by cancelling the request task when its patience runs out, which propagates to the upstream stream.

## D8. Deterministic code instead of model calls for analysis and verification (Assistant)
- At ~2K tokens per answer and 8K TPM, every extra model call reduces capacity by roughly half.
- Trade-off: rule-based verification cannot judge paraphrased meaning; it can catch hallucinated numbers, wrong citations and missing support. Known gap, stated in the report.

## D9. Things deliberately not built (user to confirm for report heading 8)
- Dense-embedding / hybrid retrieval: BM25 + cross-encoder already finds the expected section for 16/16 answerable questions with `medium_k4_rr` (retrieval-only check), and an embedding model would add another CPU cost next to the re-ranker.
- Answer caching (semantic or exact): the evaluation asks each question once per setting, so a cache would hide the cost being measured.
- Multi-model routing / provider failover: a second model or provider would mean a second set of limits to measure; the brief asks to work inside one free tier.

## D10. Percentiles
- Nearest-rank (returns an observed value, never an interpolated one). Failed requests are never silently dropped: first-token percentiles are over `ok` requests (the only ones with a first token, D14) and total-time percentiles over every request; each table states its population and `n`.

## D11. Stand-in answers are extractive, not generated (Assistant)
- The stand-in quotes the first sentence of each passage it receives and cites it as `[Pn]`; with no passages it answers `NOT_IN_PAPERS`.
- Why: load tests need output that passes through the same verification step as real answers, with realistic length and timing, at zero cost. Faking "intelligent" answers would add nothing measurable.
- Cost: answer quality and refusal behaviour from stand-in runs mean nothing; `answer_grade` is left empty for them.

## D12. Stand-in calibration is checked, not just fitted (Assistant)
- `experiments/standin_check.py` replays the real probe shapes against the stand-in. It caught two calibration errors (constant header→token gap; RPM burst 30 instead of a ~22-request bucket). Both were fixed from the raw logs.
- It compared first-token times only, so it missed a third error: the stand-in streamed ~4x slower than calibrated after the first token (Windows timer rounding of short sleeps). Found from the load-run traces in Phase 13, fixed, tested, and all stand-in load runs re-run (EXPERIMENTS.md).

## D13. Degraded mode cuts prompt tokens, never re-ranking or verification (Assistant, Phase 5)
- Degraded (TPM headroom < 25 % or ≥ 4 waiting): 2 passages instead of 4, no regeneration.
- Re-ranking stays on because it costs CPU, not provider budget, and its score drives the refuse-before-model branch. Evidence: in the first stand-in smoke run, degraded mode also dropped re-ranking and "What is the capital of France?" was answered from Moshi's Appendix F transcripts; with re-ranking kept it is refused before any model call (score −10.4).
- The re-ranker sheds load itself: one runs at a time, at most 2 wait, the rest use BM25 order.

## D14. Status mapping and when a request is turned away (Assistant, Phase 5)
- A refusal backed by evidence ("the papers don't say") is status `ok` — it is the correct product behaviour. Being turned away for capacity, or getting a passages-only reply because the daily budget is gone, is `rate_limited`. Full table in ARCHITECTURE.md §2.
- `first_token_ms` is filled only for `ok`; capacity/timeout/error messages are not counted as a first token.
- Requests are turned away at arrival when the estimated wait cannot fit their deadline, rather than queued to fail later. Evidence (stand-in, limits halved without notice, 12 simultaneous requests): 2 TPM 429s before the client learned the new limit from headers, then 7 requests turned away at arrival with no further 429s.

## D15. Raw runs are immutable; results.csv is assembled from selected runs (Assistant, Phase 8)
- Every experiment run writes to its own `results/runs/<run_id>/` and is never edited (except `answer_grade`, copied from the hand-grading sheet by `experiments.apply_grades`).
- `results/selected_runs.json` names one run per scenario (and per retrieval setting); `python -m experiments.runs` rebuilds `results.csv` and `results/traces.jsonl` from them and rejects any run whose columns differ from the required 12.
- Why: re-running a scenario after a fix must not silently mix old and new rows, and superseded runs stay available as evidence (e.g. the first `overload` and `limit_halved` runs, which turned out too gentle).
- Alternative rejected: appending every run to one CSV and filtering later — easy to double-count.

## D16. Load-test assumptions (Assistant, Phase 8)
- Patience: 20 % of people give up after 8–15 s, the rest wait for the 30 s deadline. Not measured; a stated assumption. Each person's patience is recorded in their trace (`client` event).
- The load generator runs in the same process as the pipeline (D7), so client-side overhead is not on the network path.
- The re-ranker and index are loaded before the clock starts.
- Retrieval evaluation sends one question at a time and waits, outside the request clock, until our limiter has room; otherwise Table 3 latencies would measure the per-minute token budget rather than the retrieval setting. The waiting total is in `meta.json` (`pacing_wait_s`).
- Real runs never start without `--yes`; without it the scripts print a token estimate built from the exact prompts (`experiments/budget.py`).
- Retrieval evaluation runs with degraded mode off (`Pipeline(allow_degraded=False)`) and aborts if any request runs degraded; otherwise the three settings are not compared (the first real attempt was invalid for this reason, EXPERIMENTS.md "R5 attempt 1").

## D17. Real runs only on the user's own Groq account (Assistant raised the brief's rule; User decided, 11 Nov)
- After the first real retrieval evaluation used most of the day's tokens, a key from a second Groq account was offered. The brief's ground rules forbid extra accounts ("Do not use extra accounts or any other trick to get around its limits"), and limits are per organization, so the runs wait for the own account's quota instead.
