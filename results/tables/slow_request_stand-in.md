**Slow request 63c7cc949372** (stand-in, scenario `overload`, question U5, setting `medium_k4_rr`, degraded; run `20261111T151743Z_overload_stand-in`)

Total 28959 ms. Time before the person saw the first token: 28959 ms; after it: 0 ms (the whole answer is verified before it is shown, D5). 3 requests were already waiting when it reached the model gate; 0 retries.

| Stage | ms | share |
|---|---|---|
| analyze | 0 | 0.0 % |
| retrieval (tool) | 1507 | 5.2 % |
| queue wait (gate) | 23432 | 80.9 % |
| model, before its first token | 3951 | 13.6 % |
| model, first token to end | 65 | 0.2 % |
| failed attempts | 0 | 0.0 % |
| retry backoff | 0 | 0.0 % |
| verification | 2 | 0.0 % |
| other (scheduling, delivery) | 2 | 0.0 % |

| Group | ms | share |
|---|---|---|
| waiting | 23432 | 80.9 % |
| processing | 5525 | 19.1 % |
| retrying | 0 | 0.0 % |

Bottleneck: **queueing**. Model call: response headers after 3859 ms, first answer token after 3951 ms, 830 tokens in / 66 out (stand-in marked this call as a simulated slow start).
