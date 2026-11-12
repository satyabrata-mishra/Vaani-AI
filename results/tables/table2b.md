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
