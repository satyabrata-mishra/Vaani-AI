# Decisions log

Each entry: decision, alternatives, trade-off, who decided. "User" = Subhrajeet; "Assistant" = proposed by the AI assistant and not yet challenged.

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

## D5. Verify-then-release per sentence (Assistant — candidate for user review)
- See ARCHITECTURE.md §2. Costs some time to first token; guarantees that no unverified sentence is shown.
- `first_token_ms` = arrival → first verified token delivered to the user, including queue wait.

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

## D9. Things deliberately not built (draft, user to confirm)
- Dense-embedding / hybrid retrieval.
- Semantic answer caching (only exact-question caching of already verified answers is considered).
- Multi-model routing / provider failover.

## D10. Percentiles
- Nearest-rank method on all requests in the population named in each table caption. Failed requests are never silently dropped. Latency percentiles are computed over requests that produced at least one token for first-token latency, and over all completed requests for total time; the population size `n` is printed next to each percentile.
