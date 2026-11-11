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

## Run the assistant

```powershell
$env:BACKEND = "standin"; python -m uvicorn app.main:app --port 8000   # stand-in must be running on 8100
$env:BACKEND = "real";    python -m uvicorn app.main:app --port 8000   # Groq, key from .env (spends quota)
```

`POST /ask {"question": "...", "retrieval_setting": "medium_k4_rr"}` streams server-sent events: `token` events with
the verified answer, then one `done` event with status, outcome and sources. `GET /health` shows queue and budget state.
Every request appends one row to `results.csv` and one trace to `results/traces.jsonl`.

`python -m experiments.pipeline_smoke` runs all questions in `data/questions.json` through the pipeline against an
in-process stand-in (no quota, nothing written to `results.csv`).

## Experiments

```powershell
python -m experiments.load_test all                        # five stand-in load scenarios (~32 min, no quota)
python -m experiments.load_test burst --seed 1             # one scenario, another seed
python -m experiments.retrieval_eval                       # dry run: real-Groq token budget for Table 3
python -m experiments.retrieval_eval --yes                 # real Groq: 21 questions x 3 settings (spends quota)
python -m experiments.load_test steady_real --backend real # dry run; add --yes for the real baseline
python -m experiments.apply_grades                         # copy hand grades from results/grading/*.csv
python -m experiments.runs                                 # rebuild results.csv from results/selected_runs.json
python -m app.evaluation.report                            # Tables 1, 2a, 2b, 3 -> results/tables/
python -m app.evaluation.report --check                    # verify tables + results.csv regenerate exactly
```

Each run keeps its raw data in `results/runs/<run_id>/`; the root `results.csv` is rebuilt from the runs named in
`results/selected_runs.json`, so it can always be regenerated. Real runs never start without `--yes`.

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
