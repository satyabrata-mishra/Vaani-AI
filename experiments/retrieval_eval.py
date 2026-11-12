"""Ask every question in data/questions.json under each retrieval setting, through the complete
pipeline, and write a grading sheet for hand grading (D3).

    python -m experiments.retrieval_eval                         # dry run: real-Groq token budget
    python -m experiments.retrieval_eval --yes                   # real Groq (spends quota)
    python -m experiments.retrieval_eval --backend standin       # plumbing check, no quota
    python -m experiments.retrieval_eval --settings small_k6 --questions A1,U2 --yes

Questions are sent one at a time. Before each one the script waits (outside the request's clock)
until our own limiter has room, so the latencies compare retrieval settings rather than how long
a request waited behind the previous one for the per-minute token budget. That waiting is
recorded in meta.json as `pacing_wait_s`.

Grading: fill the `grade` column in results/grading/<run_id>.csv with one of
correct / wrong / refused_correctly / refused_wrongly, then run `python -m experiments.apply_grades`."""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from datetime import datetime, timezone

from app.config import RESULTS_DIR, RETRIEVAL_SETTINGS, load_settings
from app.infra.metrics import ResultsWriter
from app.pipeline.orchestrator import Pipeline
from app.pipeline.retrieval import get_reranker
from app.providers.openai_compat import OpenAICompatProvider
from app.simulator.runner import StandInThread
from experiments.budget import estimate
from experiments.load_test import load_questions
from experiments.runs import assemble, git_info, new_run_dir, select_run

GRADING_DIR = RESULTS_DIR / "grading"
SCENARIO = "retrieval_eval"
SHEET_COLUMNS = ["request_id", "question_id", "kind", "retrieval_setting", "question", "status", "outcome",
                 "answer", "sources", "key_facts", "expected_locations", "grade", "grader_note"]


async def run_setting(setting: str, questions: list[dict], backend: str, deadline_s: float,
                      select: bool = True) -> str:
    source = "real" if backend == "real" else "stand-in"
    run_id, d = new_run_dir(f"{SCENARIO}_{setting}", source)
    st = None
    if backend == "real":
        settings = load_settings()
        provider = OpenAICompatProvider.groq(settings)
    else:
        st = StandInThread("normal").start()
        settings = load_settings(standin_base_url=st.api_base)
        provider = OpenAICompatProvider.standin(settings)
    # Degraded mode would replace every setting's piece count with 2 and make the settings
    # indistinguishable (it did in the first real run), so it is off here.
    pipe = Pipeline(settings, provider, writer=ResultsWriter(d / "results.csv", d / "traces.jsonl"),
                    retrieval_setting=setting, allow_degraded=False)
    get_reranker()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    pacing = 0.0
    sheet = []
    try:
        for q in questions:
            est = 1700 if setting != "large_k2_rr" else 1500
            t = time.monotonic()
            while (w := pipe.limiter.wait_needed(est)) > 0:
                await asyncio.sleep(w)
            pacing += time.monotonic() - t
            tr = await pipe.handle(q["question"], scenario=SCENARIO, question_id=q["id"],
                                   retrieval_setting=setting, deadline_s=deadline_s, run_id=run_id)
            if tr.degraded:
                raise RuntimeError(f"{q['id']} ran degraded; the settings comparison would be invalid")
            if tr.outcome.startswith("daily_budget_exhausted"):
                raise RuntimeError(f"{q['id']}: daily budget exhausted ({tr.outcome}); stop and resume another day")
            print(f"  [{setting}] {q['id']:<3} {tr.status:<12} {tr.outcome:<24} "
                  f"in={tr.tokens_in:>5} out={tr.tokens_out:>4} total={tr.total_ms:>7.0f}ms", flush=True)
            sheet.append({
                "request_id": tr.request_id, "question_id": q["id"], "kind": q["kind"], "retrieval_setting": setting,
                "question": q["question"], "status": tr.status, "outcome": tr.outcome, "answer": tr.answer,
                "sources": " | ".join(tr.citations), "key_facts": " ; ".join(q["key_facts"]),
                "expected_locations": " ; ".join(f"{l['paper']} §{l.get('section', '?')} p.{l.get('page', '?')}"
                                                 for l in q["locations"]),
                "grade": "", "grader_note": ""})
    finally:
        if st:
            st.stop()
        await provider.aclose()

    GRADING_DIR.mkdir(parents=True, exist_ok=True)
    with (GRADING_DIR / f"{run_id}.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        w.writerows(sheet)
    (d / "meta.json").write_text(json.dumps({
        "run_id": run_id, "scenario": {"name": SCENARIO, "retrieval_setting": setting, "deadline_s": deadline_s},
        "source": source, "started_utc": started, "questions": [q["id"] for q in questions],
        "pacing_wait_s": round(pacing, 1), "retries_granted": pipe.retry.retries_granted,
        "peak_queue": pipe.gate.peak_waiting, "settings": settings.public_dict(), "git": git_info(),
    }, indent=2), encoding="utf-8")
    if select:
        select_run(f"{SCENARIO}:{setting}|{source}", run_id)
        assemble()
    print(f"[{setting}] grading sheet: results/grading/{run_id}.csv")
    return run_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=["real", "standin"], default="real")
    ap.add_argument("--settings", default=",".join(RETRIEVAL_SETTINGS))
    ap.add_argument("--questions", default="", help="comma-separated ids (default: all)")
    ap.add_argument("--deadline", type=float, default=60.0)
    ap.add_argument("--yes", action="store_true", help="really spend Groq quota (backend real)")
    ap.add_argument("--no-select", action="store_true", help="keep the run but do not put it in results.csv")
    a = ap.parse_args()
    settings = [s for s in a.settings.split(",") if s]
    qs = load_questions()
    if a.questions:
        wanted = set(a.questions.split(","))
        qs = [q for q in qs if q["id"] in wanted]
    if a.backend == "real":
        total = 0
        for s in settings:
            est = estimate([q["question"] for q in qs], s)
            total += est.total
            print(f"[{s}] real Groq budget: {est.line()}")
        print(f"total ~{total:,} tokens (daily token limit assumed 200,000; see Table 1)")
        if not a.yes:
            print("dry run only; add --yes to send these requests to Groq")
            return
    for s in settings:
        asyncio.run(run_setting(s, qs, a.backend, a.deadline, select=not a.no_select))


if __name__ == "__main__":
    main()
