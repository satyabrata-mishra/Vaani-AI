"""Drive the complete pipeline with a load scenario and record every request.

    python -m experiments.load_test steady                      # stand-in (no quota)
    python -m experiments.load_test all                         # the five stand-in scenarios, one after another
    python -m experiments.load_test steady_real --backend real  # dry run: prints the token budget only
    python -m experiments.load_test steady_real --backend real --yes

Arrivals, questions and patience come from one seeded RNG, so a run is reproducible given the
seed (latencies still vary with the machine). Each simulated person gives up after their
patience by cancelling their request, exactly like a client disconnect.

Outputs go to results/runs/<run_id>/ (see experiments/runs.py); the run is then selected for its
scenario and the root results.csv is re-assembled."""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from datetime import datetime, timezone

import httpx

from app.config import DATA_DIR, load_settings
from app.infra.metrics import ResultsWriter
from app.infra.retry import RetryPolicy
from app.pipeline.orchestrator import Pipeline
from app.pipeline.retrieval import get_reranker, retrieve_sync
from app.providers.openai_compat import OpenAICompatProvider
from app.simulator.runner import StandInThread
from experiments.budget import estimate
from experiments.runs import assemble, git_info, new_run_dir, select_run
from experiments.scenarios import REAL_SCENARIOS, RETRY_SCENARIOS, SCENARIOS, Scenario


def load_questions() -> list[dict]:
    return json.loads((DATA_DIR / "questions.json").read_text(encoding="utf-8"))["questions"]


def plan(sc: Scenario, seed: int) -> list[tuple[float, dict, float]]:
    """(arrival offset s, question, patience s) for every request, from one seeded RNG."""
    rng = random.Random(seed)
    qs = load_questions()
    return [(t, rng.choice(qs), sc.patience.draw(rng)) for t in sc.arrivals(rng)]


async def _person(pipe: Pipeline, sc: Scenario, q: dict, patience: float, run_id: str, counts: dict):
    task = asyncio.create_task(pipe.handle(q["question"], scenario=sc.name, question_id=q["id"],
                                           retrieval_setting=sc.retrieval_setting, deadline_s=sc.deadline_s,
                                           run_id=run_id, client={"patience_s": round(patience, 2)}))
    done, _ = await asyncio.wait({task}, timeout=patience)
    if not done:
        task.cancel()
        counts["gave_up"] += 1
        try:
            await task
        except asyncio.CancelledError:
            pass
        return
    t = task.result()
    counts[t.status] = counts.get(t.status, 0) + 1


async def _monitor(pipe: Pipeline, path, t0: float, stop: asyncio.Event):
    with path.open("w", encoding="utf-8") as f:
        while not stop.is_set():
            f.write(json.dumps({
                "t_s": round(time.monotonic() - t0, 2), "waiting": pipe.gate.waiting,
                "in_flight": pipe.gate.in_flight, "tpm_headroom": round(pipe.limiter.tpm_headroom(), 3),
                "rerank_waiting": pipe.tool.rerank_waiting, "daily_blocked": pipe.limiter.daily_blocked}) + "\n")
            f.flush()
            try:
                await asyncio.wait_for(stop.wait(), 1.0)
            except asyncio.TimeoutError:
                pass


async def _switch(base: str, sc: Scenario, t0: float, events: list):
    await asyncio.sleep(max(0.0, sc.switch_at_s - (time.monotonic() - t0)))
    async with httpx.AsyncClient() as c:
        st = (await c.post(f"{base}/admin/profile", json={"name": sc.switch_to, "keep_state": True})).json()
    events.append({"t_s": round(time.monotonic() - t0, 2), "event": "provider_profile_switch",
                   "to": sc.switch_to, "limits": st["limits"]})


async def run_scenario(sc: Scenario, backend: str = "standin", seed: int = 0, scale: float = 1.0,
                       select: bool = True, verbose: bool = True) -> str:
    requests = plan(sc, seed)
    source = "real" if backend == "real" else "stand-in"
    run_id, d = new_run_dir(sc.name, source)
    st = None
    if backend == "real":
        settings = load_settings()
        provider = OpenAICompatProvider.groq(settings)
    else:
        st = StandInThread(sc.profile, seed=seed, scale=scale).start()
        settings = load_settings(standin_base_url=st.api_base)
        provider = OpenAICompatProvider.standin(settings)
    retry = {"policy": None, "none": RetryPolicy(max_retries=0), "naive": RetryPolicy(naive=True)}[sc.retry]
    pipe = Pipeline(settings, provider, writer=ResultsWriter(d / "results.csv", d / "traces.jsonl"),
                    retrieval_setting=sc.retrieval_setting, retry=retry)

    # Load the re-ranker and index before the clock starts, so request 1 does not pay a one-off cost.
    get_reranker()
    retrieve_sync("warm up", sc.retrieval_setting)

    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"gave_up": 0}
    events: list[dict] = []
    stop = asyncio.Event()
    t0 = time.monotonic()
    mon = asyncio.create_task(_monitor(pipe, d / "timeline.jsonl", t0, stop))
    switch = asyncio.create_task(_switch(st.base, sc, t0, events)) if (sc.switch_to and st) else None
    people = []
    try:
        for i, (offset, q, patience) in enumerate(requests):
            await asyncio.sleep(max(0.0, offset - (time.monotonic() - t0)))
            people.append(asyncio.create_task(_person(pipe, sc, q, patience, run_id, counts)))
            if verbose and (i + 1) % 10 == 0:
                print(f"  [{sc.name}] t={time.monotonic() - t0:6.1f}s sent {i + 1}/{len(requests)} "
                      f"waiting={pipe.gate.waiting} in_flight={pipe.gate.in_flight} {counts}", flush=True)
        await asyncio.gather(*people)
    finally:
        wall_s = time.monotonic() - t0
        stop.set()
        await mon
        if switch:
            await switch
        standin_state = None
        if st:
            async with httpx.AsyncClient() as c:
                standin_state = (await c.get(f"{st.base}/admin/state")).json()
                (d / "standin_log.json").write_text(json.dumps((await c.get(f"{st.base}/admin/log")).json()),
                                                    encoding="utf-8")
            st.stop()
        await provider.aclose()

    meta = {
        "run_id": run_id, "scenario": sc.to_dict(), "source": source, "seed": seed, "standin_scale": scale,
        "started_utc": started, "wall_s": round(wall_s, 1), "requests": len(requests),
        "offered_per_min": round(len(requests) / (sc.duration_s / 60), 2),
        "outcomes": counts, "peak_queue": pipe.gate.peak_waiting, "peak_in_flight": pipe.gate.peak_in_flight,
        "retries_granted": pipe.retry.retries_granted, "retries_denied_budget": pipe.retry.retries_denied_budget,
        "rerank_skipped": pipe.tool.rerank_skipped, "learned_tpm_limit": pipe.limiter.observed_tpm_limit,
        "daily_blocked": pipe.limiter.daily_blocked, "events": events,
        "settings": settings.public_dict(), "git": git_info(), "standin_state": standin_state,
    }
    (d / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if select:
        select_run(f"{sc.name}|{source}", run_id)
        assemble()
    if verbose:
        print(f"[{sc.name}] done in {wall_s:.0f}s: {len(requests)} requests, {counts}, "
              f"peak queue {pipe.gate.peak_waiting} -> results/runs/{run_id}", flush=True)
    return run_id


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", help=f"one of {list(SCENARIOS) + list(RETRY_SCENARIOS) + list(REAL_SCENARIOS)}, "
                                     "'all' (the five load shapes) or 'retries' (the three retry variants)")
    ap.add_argument("--backend", choices=["standin", "real"], default="standin")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--scale", type=float, default=1.0, help="stand-in latency scale (1.0 = calibrated)")
    ap.add_argument("--no-select", action="store_true", help="keep the run but do not put it in results.csv")
    ap.add_argument("--yes", action="store_true", help="really spend Groq quota (backend real)")
    a = ap.parse_args()

    if a.scenario == "all":
        names = list(SCENARIOS)
    elif a.scenario == "retries":
        names = list(RETRY_SCENARIOS)
    else:
        names = [a.scenario]
    catalog = {**SCENARIOS, **RETRY_SCENARIOS, **REAL_SCENARIOS}
    for n in names:
        if n not in catalog:
            raise SystemExit(f"unknown scenario {n}")
    if a.backend == "real":
        for n in names:
            sc = catalog[n]
            reqs = plan(sc, a.seed)
            est = estimate([q["question"] for _, q, _ in reqs], sc.retrieval_setting)
            print(f"[{n}] real Groq budget: {est.line()}")
        if not a.yes:
            print("dry run only; add --yes to send these requests to Groq")
            return
    for n in names:
        asyncio.run(run_scenario(catalog[n], a.backend, a.seed, a.scale, select=not a.no_select))


if __name__ == "__main__":
    main()
