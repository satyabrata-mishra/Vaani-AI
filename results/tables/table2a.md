**Table 2a - Latency per scenario. First-token percentiles: requests with status ok only (n shown); first token = arrival to the first token of the verified answer, queue wait included. Total-time percentiles: every request, whatever its status. Duration: first arrival to last completion. Stand-in rows are simulated and must not be read as Groq measurements.**

| Scenario | Source | Requests | Duration (ms) | Offered req/min | First token p50 | p95 | p99 | n (first token) | Total p50 | p95 | p99 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| steady | stand-in | 31 | 562199 | 3.1 | 2592 | 5952 | 6024 | 31 | 2592 | 5952 | 6024 |
| burst | stand-in | 29 | 186751 | 7.2 | 2525 | 28740 | 28740 | 18 | 2144 | 21563 | 28740 |
| overload | stand-in | 87 | 319830 | 17.4 | 16391 | 27743 | 29143 | 57 | 12724 | 26139 | 29143 |
| limit_halved | stand-in | 40 | 470918 | 5.0 | 2748 | 7449 | 8841 | 40 | 2748 | 7449 | 8841 |
| daily_exhausted | stand-in | 23 | 279073 | 4.6 | 2849 | 5710 | 5952 | 20 | 2854 | 5710 | 5952 |
