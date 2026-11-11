# The Relay

A small, fault-tolerant research assistant that answers questions from three arXiv papers
(1706.03762v7, 2410.00037v1, 2507.07955v2) with cited answers, running on the Groq free tier
and on a stand-in model service for load tests.

Design: `docs/ARCHITECTURE.md`. Decisions: `docs/DECISIONS.md`. Requirement mapping: `docs/ASSIGNMENT_ANALYSIS.md`.

## Setup

```powershell
python -m pip install -r requirements.txt
copy .env.example .env      # then put your own GROQ_API_KEY in .env (never commit it)
python scripts/fetch_papers.py
```

## Stand-in model service

```powershell
python -m app.simulator.calibration                       # fit from results/real_probe/ (already committed)
python -m app.simulator.server --profile normal --port 8100
python -m experiments.standin_check                       # compare stand-in vs real probe shapes
```

Profiles: `normal`, `high_latency`, `flaky`, `rate_limited`, `daily_exhausted`, `heavy_concurrency`, `overload`, `unlimited`.
Switch at runtime: `POST http://127.0.0.1:8100/admin/profile {"name": "rate_limited"}`; inspect with `GET /admin/state` and `/admin/log`.

## Tests

```powershell
python -m pytest            # never uses real Groq quota; tests marked `real` are skipped by default
```

## Layout

| Path | Contents |
|---|---|
| `app/config.py` | all settings, retrieval settings, CSV schema, prices |
| `app/ingest/` | PDF parsing, chunking, BM25 index |
| `app/pipeline/` | the six pipeline steps and the orchestrator |
| `app/providers/` | one OpenAI-compatible client used for both Groq and the stand-in |
| `app/infra/` | admission queue, rate limiter, retries, deadlines, metrics |
| `app/simulator/` | the stand-in model service |
| `app/evaluation/` | questions, grading, report tables |
| `experiments/` | real-API probes, calibration, load tests, retrieval evaluation |
| `papers/` | the three PDFs and their checksums |
| `results/`, `results.csv` | raw measurements |
