**Slow request 49eb48847d50** (real, scenario `retrieval_eval`, question H5, setting `medium_k4_rr`; run `20261112T125742Z_retrieval_eval_medium_k4_rr_real`)

Total 17628 ms. Time before the person saw the first token: 17628 ms; after it: 0 ms (the whole answer is verified before it is shown, D5). 0 requests were already waiting when it reached the model gate; 1 retries.

| Stage | ms | share |
|---|---|---|
| analyze | 0 | 0.0 % |
| retrieval (tool) | 1536 | 8.7 % |
| queue wait (gate) | 4451 | 25.2 % |
| model, before its first token | 5293 | 30.0 % |
| model, first token to end | 117 | 0.7 % |
| failed attempts | 6010 | 34.1 % |
| retry backoff | 202 | 1.1 % |
| verification | 1 | 0.0 % |
| other (scheduling, delivery) | 18 | 0.1 % |

| Group | ms | share |
|---|---|---|
| waiting | 4451 | 25.2 % |
| processing | 6947 | 39.4 % |
| retrying | 6212 | 35.2 % |

Bottleneck: **retrying**. Model call: response headers after 5152 ms, first answer token after 5293 ms, 1637 tokens in / 124 out.
