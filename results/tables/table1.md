**Table 1 - Real service limits (openai/gpt-oss-20b, Groq free tier, measured 2026-11-11). Source: results/real_probe/.**

| Limit | Value | How measured | Confidence |
|---|---|---|---|
| Requests per minute | 30 accepted, then 429 "(RPM): Limit 30" | 2 bursts of tiny requests every 0.5 s until 429 (R2) | high (measured twice) |
| Tokens per minute | 8000 (header); 429 after 6904 tokens in 1.9 s | ~1.7K-token requests back-to-back until 429 (R3); 429 compares the new prompt with what is left | high (measured) |
| Requests per day | 1000 (header); limit never reached | `x-ratelimit-limit-requests`; refill check: 98 used by header vs 100 sent over 18 min | medium (header value; reaching it was deliberately not attempted) |
| Tokens per day | 200000 (documentation); no header, never reached | Groq rate-limits page | low (not measured) |
| Requests in flight at once | >= 8 (8/8 simultaneous succeeded) | 8 requests started within 62 ms (R4); more was not tried (RPM would reject first) | medium (lower bound) |
| Behaviour at a limit | HTTP 429 in 79 / 63 ms (p50 per burst), message names the limit, `retry-after` 2 s; rejected requests not counted against the day | R2/R3 429 bodies and headers | high |
| Time until recovery | RPM: 4547, 4500 ms after first 429; TPM: 3672 ms; per day: not measured | poll every 3 s (or after retry-after) until a 200 | medium (n = 3 events) |
