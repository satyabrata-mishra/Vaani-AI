**Slow request 83c33ab04b00** (stand-in, scenario `overload`, question U5, setting `medium_k4_rr`, degraded; run `20261111T123719Z_overload_stand-in`)

Total 29143 ms. Time before the person saw the first token: 29143 ms; after it: 0 ms (the whole answer is verified before it is shown, D5). 3 requests were already waiting when it reached the model gate; 0 retries.

| Stage | ms | share |
|---|---|---|
| analyze | 0 | 0.0 % |
| retrieval (tool) | 1506 | 5.2 % |
| queue wait (gate) | 23483 | 80.6 % |
| model, before its first token | 3947 | 13.5 % |
| model, first token to end | 204 | 0.7 % |
| failed attempts | 0 | 0.0 % |
| retry backoff | 0 | 0.0 % |
| verification | 1 | 0.0 % |
| other (scheduling, delivery) | 2 | 0.0 % |

| Group | ms | share |
|---|---|---|
| waiting | 23483 | 80.6 % |
| processing | 5658 | 19.4 % |
| retrying | 0 | 0.0 % |

Bottleneck: **queueing**. Model call: response headers after 3844 ms, first answer token after 3947 ms, 830 tokens in / 66 out (stand-in marked this call as a simulated slow start).
