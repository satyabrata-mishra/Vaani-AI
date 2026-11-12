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
