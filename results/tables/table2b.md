**Table 2b - Outcomes per scenario. Percentages over all requests sent. Tokens per answer = all tokens the scenario spent (including failed attempts) / ok requests. Cost at Groq list price (USD 0.075 / 1M input, 0.30 / 1M output; the free tier itself costs 0). Stand-in token counts are the stand-in's estimates.**

| Scenario | Source | Requests | Success % | Rate-limited % | Timeout / gave up % | Error % | Retries per request | Peak queue | Degraded % | Tokens per answer | USD per 1,000 answers |
|---|---|---|---|---|---|---|---|---|---|---|---|
| steady | stand-in | 31 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.00 | 1 | 0.0 | 1478 | 0.149 |
| burst | stand-in | 29 | 62.1 | 37.9 | 0.0 (0 gave up) | 0.0 | 0.00 | 5 | 41.4 | 1262 | 0.129 |
| overload | stand-in | 87 | 65.5 | 18.4 | 16.1 (14 gave up) | 0.0 | 0.00 | 5 | 94.3 | 865 | 0.088 |
| limit_halved | stand-in | 40 | 100.0 | 0.0 | 0.0 (0 gave up) | 0.0 | 0.00 | 1 | 40.0 | 1231 | 0.125 |
| daily_exhausted | stand-in | 23 | 87.0 | 13.0 | 0.0 (0 gave up) | 0.0 | 0.00 | 1 | 0.0 | 1534 | 0.155 |
