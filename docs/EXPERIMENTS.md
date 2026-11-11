# Experiments — plan (to be filled with results)

## Real-API budget plan (needs user approval before each run)

Account limits for `openai/gpt-oss-20b` *(doc, not yet measured)*: 30 RPM, 8K TPM, 1K RPD, 200K TPD.
Everything we run shares these, including any manual testing in the Groq console.

| Run | Purpose | Requests | Est. tokens | When |
|---|---|---|---|---|
| R0 | Smoke test: one tiny call, read headers | 1 | ~100 | Day 1 |
| R1 | Latency baseline at 3 prompt sizes (~300 / ~1.5K / ~3K input tokens), sequential, spaced to stay under TPM | 24 | ~45K | Day 1 |
| R2 | Per-minute limit probe: tiny requests (`max_tokens` = 1) sent fast until 429; then poll for recovery | ~40 | ~2K | Day 1 |
| R3 | TPM probe: ~2K-token requests back-to-back until 429; read `retry-after` and reset headers | ~6 | ~12K | Day 1 |
| R4 | Concurrency probe: 8 tiny requests in parallel | 8 | ~400 | Day 1 |
| R5 | Retrieval settings: 3 settings x ~20 questions, throttled to stay under TPM | ~60–70 | ~120K | Day 2 (fresh daily window) |
| R6 | Small real steady-load row: ~15 questions at ~3/min | ~15 | ~30K | Day 2, only if budget remains |

Total ~= 60K tokens on day 1 and ~150K on day 2: inside 200K TPD per day, with headroom for mistakes.
We will not deliberately exhaust the real daily quota; the daily-exhausted scenario runs on the stand-in, and any real daily 429 we happen to see is recorded as evidence.

## Load scenarios (stand-in)
To be designed in Phase 8: steady, burst, sustained overload, per-minute limit halved, daily budget exhausted.

## Retrieval settings
To be fixed after the parser is built and chunk sizes are measured.

## Candidate ways this library misleads a naive retriever (hypotheses — to be verified against the actual text)
1. Shared vocabulary across papers: "attention", "Transformer", "chunk", "tokenizer" appear in all three; questions without a paper name can pull passages from the wrong paper.
2. Reference lists: Moshi (references from p.42) and H-Net (from p.25) cite other papers' titles and claims; a keyword match on a cited title returns a bibliography line instead of content.
3. Tables flattened by PDF extraction: numeric results lose their row/column labels, so a retrieved number can be attached to the wrong model or setting.
